#!/usr/bin/env python3
"""
Python Email Fetcher Service
Replaces php-imap functionality with proxy support for HTTP/SOCKS5
"""

import json
import sqlite3
import sys
import os
import base64
import email
import socket
import ssl
import imaplib
import socks
import http.client
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Check and install missing dependencies
def check_dependencies():
    """Check if all required dependencies are installed and attempt to install if missing"""
    missing_deps = []
    
    try:
        import imapclient
    except ImportError:
        missing_deps.append('imapclient')
    
    try:
        import requests
    except ImportError:
        missing_deps.append('requests')
    
    try:
        import socks
    except ImportError:
        missing_deps.append('pysocks')
    
    if missing_deps:
        logger.warning(f"Missing dependencies: {missing_deps}")
        logger.info("Attempting to install missing dependencies...")
        
        try:
            import subprocess
            import sys
            
            for dep in missing_deps:
                logger.info(f"Installing {dep}...")
                result = subprocess.run([
                    sys.executable, '-m', 'pip', 'install', '--user', dep
                ], capture_output=True, text=True, timeout=60)
                
                if result.returncode != 0:
                    logger.error(f"Failed to install {dep}: {result.stderr}")
                    return False
                else:
                    logger.info(f"Successfully installed {dep}")
            
            # Try importing again after installation
            if 'imapclient' in missing_deps:
                import imapclient
            if 'pysocks' in missing_deps:
                import socks
            if 'requests' in missing_deps:
                import requests
                
            logger.info("All dependencies installed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to install dependencies: {e}")
            return False
    
    return True

# Initialize dependencies
if not check_dependencies():
    print(json.dumps({
        'success': False,
        'message': '系统依赖包安装失败，请确保在root环境下运行或手动安装依赖包'
    }, ensure_ascii=False))
    sys.exit(1)

class ProxyMailFetcher:
    def __init__(self, server, port, username, password, protocol='imap', use_ssl=True):
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.protocol = protocol.lower()
        self.use_ssl = use_ssl
        self.connection = None
        self.proxy_enabled = False
        self.proxy_info = None
        
        # 性能优化：连接缓存
        self._connection_cache = {}
        self._last_connection_time = None
        self._connection_timeout = 300  # 5分钟连接超时
        
        # Check and configure proxy settings
        self._check_proxy_status()
        
    def _get_cache_key(self):
        """生成连接缓存键"""
        proxy_key = ""
        if self.proxy_enabled and self.proxy_info:
            proxy_key = f"_{self.proxy_info['type']}_{self.proxy_info['host']}_{self.proxy_info['port']}"
        return f"{self.server}_{self.port}_{self.username}_{self.use_ssl}{proxy_key}"
        
    def _is_connection_expired(self):
        """检查连接是否过期"""
        if not self._last_connection_time:
            return True
        import time
        return (time.time() - self._last_connection_time) > self._connection_timeout
        
    def _check_proxy_status(self):
        """Check proxy configuration from database - 优化版本"""
        try:
            db_path = os.path.join(os.path.dirname(__file__), '..', 'db', 'mail.sqlite')
            
            if not os.path.exists(db_path):
                return
                
            # 性能优化：缓存数据库连接
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            try:
                # Check if proxy_config table exists
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='proxy_config'")
                if not cursor.fetchone():
                    return
                
                # Get proxy configuration with single query
                cursor.execute("""
                    SELECT config_key, config_value FROM proxy_config 
                    WHERE config_key IN ('proxy_enabled', 'active_proxy_type', 'active_proxy_id')
                """)
                
                config = {row[0]: row[1] for row in cursor.fetchall()}
                
                # Check if proxy is enabled
                if config.get('proxy_enabled') == '1':
                    proxy_type = config.get('active_proxy_type', '')
                    proxy_id = int(config.get('active_proxy_id', '0'))
                    
                    if proxy_type and proxy_id > 0:
                        # Get proxy details
                        table_name = 'socks5_proxies' if proxy_type == 'socks5' else 'http_proxies'
                        
                        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                        if cursor.fetchone():
                            cursor.execute(f"SELECT * FROM {table_name} WHERE id = ? AND status = 1", (proxy_id,))
                            proxy = cursor.fetchone()
                            
                            if proxy:
                                # Map database columns to proxy info
                                columns = [desc[0] for desc in cursor.description]
                                proxy_dict = dict(zip(columns, proxy))
                                
                                self.proxy_enabled = True
                                self.proxy_info = {
                                    'type': proxy_type,
                                    'host': proxy_dict.get('host', ''),
                                    'port': proxy_dict.get('port', 0),
                                    'username': proxy_dict.get('username', ''),
                                    'password': proxy_dict.get('password', ''),
                                    'name': proxy_dict.get('name', '')
                                }
                                
                                logger.info(f"Proxy configured: {proxy_type} - {self.proxy_info['name']}")
            finally:
                conn.close()
            
        except Exception as e:
            logger.error(f"Error checking proxy status: {e}")
            # Don't fail on proxy configuration errors
            
    def get_proxy_info(self):
        """Get proxy information for response - only exposes safe fields"""
        safe_info = None
        if self.proxy_info:
            safe_info = {
                'name': self.proxy_info.get('name', ''),
                'type': self.proxy_info.get('type', '')
            }
        return {
            'enabled': self.proxy_enabled,
            'info': safe_info
        }
        
    def connect(self):
        """Connect to mail server"""
        connection_method = "未知"
        try:
            if self.proxy_enabled:
                connection_method = f"代理 ({self.proxy_info['type']} - {self.proxy_info['name']})"
                logger.info(f"Attempting connection with proxy: {self.proxy_info['name']} ({self.proxy_info['type']})")
                return self._connect_with_proxy()
            else:
                connection_method = "直连"
                logger.info("Connecting directly to mail server")
                
            if self.protocol == 'imap':
                return self._connect_imap()
            elif self.protocol == 'pop3':
                # POP3 support can be added later if needed
                raise Exception("POP3 protocol not yet implemented in Python version")
            else:
                raise Exception(f"Unsupported protocol: {self.protocol}")
                
        except Exception as e:
            error_message = str(e)
            
            # Don't double-wrap error messages that already contain connection info
            # Check for various patterns including parentheses to avoid duplication
            if not any(pattern in error_message for pattern in ["代理", "直连", "(代理)", "(直连)", "通过代理", "通过直连"]):
                if self.proxy_enabled:
                    error_message += f" (通过{connection_method}连接)"
                else:
                    error_message += f" ({connection_method})"
            
            logger.error(f"Connection failed: {error_message}")
            raise Exception(error_message)
    
    def _connect_with_proxy(self):
        """Connect to mail server through proxy"""
        proxy_type = self.proxy_info['type']
        proxy_host = self.proxy_info['host']
        proxy_port = self.proxy_info['port']
        proxy_username = self.proxy_info.get('username', '')
        proxy_password = self.proxy_info.get('password', '')
        
        try:
            if proxy_type == 'socks5':
                return self._connect_socks5_proxy(proxy_host, proxy_port, proxy_username, proxy_password)
            elif proxy_type == 'http':
                return self._connect_http_proxy(proxy_host, proxy_port, proxy_username, proxy_password)
            else:
                raise Exception(f"Unsupported proxy type: {proxy_type}")
                
        except Exception as e:
            error_message = f"Proxy connection failed ({proxy_type}): {str(e)}"
            logger.error(error_message)
            raise Exception(error_message)
    
    def _connect_socks5_proxy(self, proxy_host, proxy_port, proxy_username, proxy_password):
        """Connect using SOCKS5 proxy"""
        original_socket = socket.socket
        
        try:
            # Configure SOCKS5 proxy
            logger.info(f"Configuring SOCKS5 proxy: {proxy_host}:{proxy_port}")
            
            # Test SOCKS5 proxy connectivity first
            try:
                test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                test_socket.settimeout(10)
                
                logger.info(f"Testing connection to SOCKS5 proxy {proxy_host}:{proxy_port}")
                try:
                    test_result = test_socket.connect_ex((proxy_host, proxy_port))
                    test_socket.close()
                    
                    if test_result != 0:
                        if test_result == 111:  # Connection refused
                            raise Exception(f"SOCKS5代理服务器 {proxy_host}:{proxy_port} 连接被拒绝 - 请检查代理服务器状态")
                        else:
                            raise Exception(f"无法连接到SOCKS5代理服务器 {proxy_host}:{proxy_port} (错误码: {test_result})")
                            
                except socket.timeout:
                    test_socket.close()
                    raise Exception(f"连接SOCKS5代理服务器 {proxy_host}:{proxy_port} 超时")
                except socket.gaierror as e:
                    test_socket.close()
                    raise Exception(f"无法解析SOCKS5代理服务器地址 {proxy_host}: {str(e)}")
                except Exception as e:
                    test_socket.close()
                    if "[Errno 111] Connection refused" in str(e):
                        raise Exception(f"SOCKS5代理服务器 {proxy_host}:{proxy_port} 连接被拒绝 - 请检查代理服务器状态")
                    else:
                        raise Exception(f"SOCKS5代理服务器连接测试失败: {str(e)}")
                    
            except Exception as e:
                # Check if it's already a translated error message
                if "SOCKS5代理" in str(e) or "连接被拒绝" in str(e) or "超时" in str(e) or "无法解析" in str(e):
                    raise e
                else:
                    raise Exception(f"SOCKS5代理服务器连接失败: {str(e)}")
            
            # Set up SOCKS5 proxy
            try:
                if proxy_username and proxy_password:
                    socks.set_default_proxy(socks.SOCKS5, proxy_host, proxy_port, 
                                           username=proxy_username, password=proxy_password)
                else:
                    socks.set_default_proxy(socks.SOCKS5, proxy_host, proxy_port)
                
                # Monkey patch socket to use SOCKS5
                socket.socket = socks.socksocket
                
                logger.info(f"SOCKS5 proxy configured successfully")
                
                # Now connect via IMAP with the proxied socket
                result = self._connect_imap()
                return result
                
            except socks.ProxyError as e:
                raise Exception(f"SOCKS5代理配置错误: {str(e)}")
            except socks.GeneralProxyError as e:
                raise Exception(f"SOCKS5代理一般错误: {str(e)}")
            except socks.ProxyConnectionError as e:
                raise Exception(f"SOCKS5代理连接错误: {str(e)}")
            except Exception as e:
                error_msg = str(e)
                if "邮箱" in error_msg or "IMAP" in error_msg:
                    # This is an IMAP-related error, not proxy error
                    raise e
                else:
                    raise Exception(f"通过SOCKS5代理连接失败: {error_msg}")
                
        except Exception as e:
            error_msg = str(e)
            if "SOCKS5代理" in error_msg or "无法连接到SOCKS5" in error_msg or "通过SOCKS5代理连接失败" in error_msg:
                raise e
            else:
                raise Exception(f"SOCKS5 proxy connection failed: {error_msg}")
        finally:
            # Always restore original socket
            socket.socket = original_socket
    
    def _connect_http_proxy(self, proxy_host, proxy_port, proxy_username, proxy_password):
        """Connect using HTTP proxy with CONNECT tunneling - Enhanced for IMAP/POP3"""
        try:
            # For HTTP proxy, we need to establish a CONNECT tunnel to the IMAP server
            logger.info(f"Establishing HTTP CONNECT tunnel via {proxy_host}:{proxy_port} for {self.protocol.upper()}")
            
            # Create a raw socket connection to the proxy
            try:
                # Create socket and connect to proxy with longer timeout for IMAP
                proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                proxy_socket.settimeout(45)  # Increased timeout for IMAP connections
                
                # Test proxy connectivity first
                logger.info(f"Testing connection to HTTP proxy {proxy_host}:{proxy_port}")
                try:
                    proxy_socket.connect((proxy_host, proxy_port))
                    logger.info("Successfully connected to HTTP proxy")
                except ConnectionRefusedError as e:
                    proxy_socket.close()
                    raise Exception(f"HTTP代理服务器拒绝连接 {proxy_host}:{proxy_port} - 请检查代理服务器是否正常运行")
                except socket.timeout:
                    proxy_socket.close()
                    raise Exception(f"连接HTTP代理服务器 {proxy_host}:{proxy_port} 超时 - 请检查网络连接和代理设置")
                except socket.gaierror as e:
                    proxy_socket.close()
                    raise Exception(f"无法解析HTTP代理服务器地址 {proxy_host}: {str(e)}")
                except Exception as e:
                    proxy_socket.close()
                    if "[Errno 111] Connection refused" in str(e):
                        raise Exception(f"HTTP代理服务器 {proxy_host}:{proxy_port} 连接被拒绝 - 请检查代理服务器状态")
                    else:
                        raise Exception(f"连接HTTP代理服务器失败: {str(e)}")
                
                # Build CONNECT request with enhanced headers for IMAP/POP3 compatibility
                connect_request = f"CONNECT {self.server}:{self.port} HTTP/1.1\r\n"
                connect_request += f"Host: {self.server}:{self.port}\r\n"
                connect_request += "User-Agent: Python-Mail-Fetcher/1.0\r\n"
                connect_request += "Proxy-Connection: keep-alive\r\n"
                connect_request += "Connection: keep-alive\r\n"
                
                # Add proxy authentication if provided
                if proxy_username and proxy_password:
                    proxy_auth = base64.b64encode(f"{proxy_username}:{proxy_password}".encode()).decode()
                    connect_request += f"Proxy-Authorization: Basic {proxy_auth}\r\n"
                
                connect_request += "\r\n"
                
                # Send CONNECT request
                logger.info(f"Sending CONNECT request to {self.server}:{self.port} for {self.protocol.upper()}")
                proxy_socket.send(connect_request.encode())
                
                # Read response with better timeout handling and full response parsing
                proxy_socket.settimeout(30)  # Set timeout for response
                response_data = b''
                start_time = time.time()
                
                while b'\r\n\r\n' not in response_data:
                    if time.time() - start_time > 25:  # Total timeout
                        break
                    try:
                        chunk = proxy_socket.recv(1024)
                        if not chunk:
                            break
                        response_data += chunk
                    except socket.timeout:
                        logger.warning("Timeout waiting for proxy response, but continuing...")
                        break
                    except Exception as e:
                        logger.warning(f"Error reading proxy response: {e}")
                        break
                    
                response = response_data.decode('utf-8', errors='ignore')
                
                # Check if connection was successful
                if not response:
                    proxy_socket.close()
                    raise Exception("HTTP代理CONNECT失败: 未收到代理服务器响应 - 可能是代理服务器不支持CONNECT方法")
                
                # Look for success status codes with more flexibility
                success_patterns = ["200 Connection established", "200 OK", "200 Tunnel established", "200 Connected"]
                is_success = any(pattern in response for pattern in success_patterns)
                
                # Also check for HTTP/1.1 200 or HTTP/1.0 200
                if not is_success:
                    is_success = ("HTTP/1.1 200" in response or "HTTP/1.0 200" in response)
                
                if not is_success:
                    proxy_socket.close()
                    response_lines = response.split('\r\n')
                    first_line = response_lines[0] if response_lines else response.strip()
                    
                    # Provide more specific error messages for IMAP/POP3
                    if "407" in first_line:
                        raise Exception(f"HTTP代理需要身份验证: {first_line} - 请检查代理用户名和密码")
                    elif "403" in first_line:
                        raise Exception(f"HTTP代理拒绝访问邮件服务器: {first_line} - 代理可能不允许连接到邮件端口")
                    elif "502" in first_line:
                        raise Exception(f"HTTP代理无法连接到邮件服务器 {self.server}:{self.port}: {first_line} - 请检查邮件服务器设置")
                    elif "504" in first_line:
                        raise Exception(f"HTTP代理连接邮件服务器超时: {first_line} - 邮件服务器可能不可达")
                    else:
                        raise Exception(f"HTTP代理CONNECT失败: {first_line} - 代理服务器不支持连接到邮件服务器端口")
                
                logger.info(f"HTTP CONNECT tunnel established successfully for {self.protocol.upper()}")
                
            except Exception as e:
                if 'proxy_socket' in locals():
                    try:
                        proxy_socket.close()
                    except:
                        pass
                # Check if it's already a translated error message
                if "HTTP代理" in str(e) or "连接被拒绝" in str(e) or "超时" in str(e) or "无法解析" in str(e):
                    raise e
                else:
                    raise Exception(f"HTTP代理连接失败: {str(e)}")
            
            # Now create IMAP connection using the tunneled socket with enhanced error handling
            try:
                if self.use_ssl:
                    # For SSL, wrap the socket with better SSL configuration
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    # Add more lenient SSL settings for email servers
                    ssl_context.set_ciphers('DEFAULT:@SECLEVEL=1')
                    
                    # Wrap the proxy socket with SSL
                    logger.info("Wrapping proxy socket with SSL for secure email connection")
                    ssl_socket = ssl_context.wrap_socket(proxy_socket, server_hostname=self.server)
                    
                    # Create IMAP connection manually with better initialization
                    self.connection = imaplib.IMAP4_SSL.__new__(imaplib.IMAP4_SSL)
                    imaplib.IMAP4.__init__(self.connection, '')
                    self.connection.sock = ssl_socket
                    self.connection.file = ssl_socket.makefile('rb')
                    
                    # Set up initial protocol state
                    self.connection.capabilities = None
                    self.connection.PROTOCOL_VERSION = 'IMAP4REV1'
                    
                    # Read the initial response from server with timeout
                    try:
                        ssl_socket.settimeout(30)  # Set timeout for SSL handshake
                        self.connection._get_response()
                        logger.info("SSL IMAP connection established successfully through HTTP proxy")
                    except Exception as e:
                        logger.warning(f"Initial server response issue (continuing): {e}")
                        # Try a simple capability check to ensure connection works
                        try:
                            self.connection.capability()
                            logger.info("IMAP capability check successful")
                        except Exception as cap_e:
                            raise Exception(f"IMAP连接失败 - SSL握手或协议错误: {str(cap_e)}")
                    
                else:
                    # For non-SSL connections (rare for modern email servers)
                    logger.info("Setting up non-SSL IMAP connection through HTTP proxy")
                    self.connection = imaplib.IMAP4.__new__(imaplib.IMAP4)
                    imaplib.IMAP4.__init__(self.connection, '')
                    self.connection.sock = proxy_socket
                    self.connection.file = proxy_socket.makefile('rb')
                    
                    # Set up initial protocol state
                    self.connection.capabilities = None
                    self.connection.PROTOCOL_VERSION = 'IMAP4REV1'
                    
                    # Read the initial response from server
                    try:
                        self.connection._get_response()
                        logger.info("Non-SSL IMAP connection established successfully through HTTP proxy")
                    except Exception as e:
                        logger.warning(f"Initial server response issue (continuing): {e}")
                
                # Authenticate with the mail server through the proxy tunnel
                try:
                    logger.info("Attempting login through HTTP proxy tunnel")
                    self.connection.login(self.username, self.password)
                    logger.info("Login successful through HTTP proxy")
                except imaplib.IMAP4.error as e:
                    error_msg = str(e).lower()
                    if "authentication" in error_msg or "login" in error_msg or "invalid" in error_msg:
                        raise Exception("邮箱用户名或密码错误，请检查登录凭据")
                    else:
                        raise Exception(f"通过HTTP代理登录失败: {str(e)}")
                except Exception as e:
                    raise Exception(f"HTTP代理隧道登录失败: {str(e)}")
                
                # Select INBOX to verify connection works
                try:
                    select_result = self.connection.select('INBOX')
                    logger.info(f"INBOX selected through HTTP proxy: {select_result}")
                except Exception as e:
                    logger.error(f"INBOX selection failed through HTTP proxy: {e}")
                    raise Exception(f"选择INBOX失败 (HTTP代理): {str(e)}")
                
                logger.info("IMAP connection through HTTP proxy established successfully")
                return True
                
            except Exception as e:
                if 'proxy_socket' in locals():
                    try:
                        proxy_socket.close()
                    except:
                        pass
                error_msg = str(e)
                if "HTTP代理" in error_msg or "代理隧道" in error_msg or "邮箱用户名或密码错误" in error_msg:
                    raise e
                else:
                    raise Exception(f"通过HTTP代理连接IMAP服务器失败: {str(e)}")
            
        except Exception as e:
            error_msg = str(e)
            if "HTTP代理" in error_msg or "IMAP服务器失败" in error_msg:
                raise e
            else:
                raise Exception(f"HTTP proxy connection failed: {error_msg}")
            
    def _connect_imap(self):
        """Connect using IMAP protocol"""
        try:
            # Set socket timeout for better error handling
            original_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(30)  # 30 second timeout
            
            try:
                # Test basic connectivity first
                logger.info(f"Testing connectivity to {self.server}:{self.port}")
                test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                test_socket.settimeout(10)
                
                try:
                    test_result = test_socket.connect_ex((self.server, self.port))
                    test_socket.close()
                    
                    if test_result != 0:
                        raise Exception(f"无法连接到邮件服务器 {self.server}:{self.port} (错误码: {test_result})")
                        
                except socket.gaierror as e:
                    raise Exception(f"无法解析邮件服务器地址 {self.server}: {str(e)}")
                except socket.timeout:
                    raise Exception(f"连接邮件服务器 {self.server}:{self.port} 超时")
                except Exception as e:
                    raise Exception(f"网络连接测试失败: {str(e)}")
                
                logger.info("Basic connectivity test passed")
                
                # Create IMAP connection
                logger.info(f"Establishing IMAP connection to {self.server}:{self.port} (SSL: {self.use_ssl})")
                
                if self.use_ssl:
                    try:
                        # Create SSL context with more permissive settings for compatibility
                        ssl_context = ssl.create_default_context()
                        ssl_context.check_hostname = False
                        ssl_context.verify_mode = ssl.CERT_NONE
                        
                        self.connection = imaplib.IMAP4_SSL(
                            self.server, 
                            self.port,
                            ssl_context=ssl_context
                        )
                    except ssl.SSLError as e:
                        raise Exception(f"SSL连接失败: {str(e)}，请检查服务器SSL配置或尝试关闭SSL")
                    except Exception as e:
                        raise Exception(f"SSL IMAP连接失败: {str(e)}")
                else:
                    try:
                        self.connection = imaplib.IMAP4(self.server, self.port)
                    except Exception as e:
                        raise Exception(f"IMAP连接失败: {str(e)}")
                
                logger.info("IMAP connection established, attempting login...")
                
                # Login with better error handling
                try:
                    self.connection.login(self.username, self.password)
                    logger.info("Login successful")
                except imaplib.IMAP4.error as e:
                    error_msg = str(e).lower()
                    if "authentication" in error_msg or "login" in error_msg or "invalid" in error_msg:
                        raise Exception("邮箱用户名或密码错误，请检查登录凭据")
                    else:
                        raise Exception(f"登录失败: {str(e)}")
                
                # Select INBOX
                try:
                    self.connection.select('INBOX')
                    logger.info("INBOX selected successfully")
                except Exception as e:
                    raise Exception(f"无法选择INBOX邮箱: {str(e)}")
                
                return True
                
            finally:
                # Restore original timeout
                socket.setdefaulttimeout(original_timeout)
                
        except Exception as e:
            error_str = str(e).lower()
            logger.error(f"IMAP connection error: {str(e)}")
            
            # Check if it's already a specific error message
            if any(keyword in str(e) for keyword in ["邮箱", "SSL连接失败", "无法连接到", "无法解析", "连接", "超时", "登录失败", "无法选择"]):
                raise e
            
            # Provide more specific error messages for generic errors
            if "authentication" in error_str or "login" in error_str or "invalid credentials" in error_str:
                raise Exception("邮箱用户名或密码错误，请检查登录凭据")
            elif "certificate" in error_str or "ssl" in error_str or "handshake" in error_str:
                raise Exception("SSL连接失败，请检查服务器SSL配置或尝试关闭SSL")
            elif "connection refused" in error_str or "no route to host" in error_str:
                raise Exception("连接被拒绝，请检查服务器地址和端口，或检查网络连接")
            elif "timeout" in error_str or "timed out" in error_str:
                raise Exception("连接超时，请检查网络连接或服务器响应")
            elif "name resolution" in error_str or "nodename nor servname" in error_str:
                raise Exception("无法解析服务器地址，请检查服务器地址是否正确")
            elif "connection reset" in error_str:
                raise Exception("连接被重置，可能是服务器或网络问题")
            else:
                raise Exception(f"IMAP连接失败: {str(e)}")
    
    def disconnect(self):
        """Disconnect from the mail server"""
        if self.connection:
            try:
                self.connection.logout()
                logger.info("Mail server connection closed")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self.connection = None
    
    def close(self):
        """Close the connection (alias for disconnect)"""
        self.disconnect()
                
    def get_latest_mail_filtered(self, filter_params=None):
        """Get emails from the mailbox with filtering support
        
        Args:
            filter_params: dict with optional keys:
                - days_filter: int, only get emails from last N days
                - sender_filter: list of allowed sender emails
                - keyword_filter: str, comma-separated keywords to search in subject
                - index: int, which email to get (0=latest, 1=second latest, etc.)
                - limit: int, maximum number of emails to return (default 1)
                - folder: str, mailbox folder to select (default INBOX)
        
        Returns:
            dict with success, message, and mail/mails data
        """
        if not self.connection:
            raise Exception("未连接到邮件服务器")
        
        filter_params = filter_params or {}
        days_filter = filter_params.get('days_filter')
        sender_filter = filter_params.get('sender_filter', [])
        keyword_filter = filter_params.get('keyword_filter', '')
        email_index = filter_params.get('index', 0)  # Which email to fetch (0=latest)
        email_limit = filter_params.get('limit', 1)  # How many emails to fetch
        folder = (filter_params.get('folder') or 'INBOX').strip() or 'INBOX'
        
        def normalize_folder_name(name):
            lower = (name or '').lower()
            if 'trash' in lower or 'deleted' in lower or 'junk' in lower or 'spam' in lower:
                return 'trash'
            return 'inbox'
        
        try:
            selected_folder = folder
            try:
                status, _ = self.connection.select(folder)
                if status != 'OK':
                    raise Exception(f"无法选择邮箱文件夹: {folder}")
            except Exception as e:
                if folder.upper() != 'INBOX':
                    # 尝试回退到收件箱，避免文件夹不可用导致失败
                    try:
                        fallback_status, _ = self.connection.select('INBOX')
                        if fallback_status != 'OK':
                            raise Exception(f"无法选择邮箱文件夹: {folder}")
                        selected_folder = 'INBOX'
                    except Exception as inner:
                        raise Exception(f"无法选择邮箱文件夹 {folder}: {inner}")
                else:
                    raise e
            
            # Search for messages with date filtering if specified
            search_criteria = ['ALL']
            
            # Add date filter if specified (only messages within X days)
            if days_filter:
                from datetime import datetime, timedelta
                since_date = datetime.now() - timedelta(days=days_filter)
                # IMAP date format: DD-Mon-YYYY
                since_date_str = since_date.strftime('%d-%b-%Y')
                search_criteria = ['SINCE', since_date_str]
            
            # Search for messages
            status, messages = self.connection.search(None, *search_criteria)
            
            if status != 'OK' or not messages[0]:
                return {
                    'success': True,
                    'message': '邮箱中没有符合条件的邮件',
                    'mail': None
                }
            
            # Get all matching message IDs
            mail_ids = messages[0].split()
            
            # If we have filtering enabled, check each message
            if sender_filter or keyword_filter:
                filtered_mail_ids = []
                
                # Process sender filter - clean and normalize email addresses (backward compatibility)
                normalized_senders = []
                if sender_filter:
                    for sender in sender_filter:
                        sender_clean = sender.strip().lower()
                        if sender_clean:
                            normalized_senders.append(sender_clean)
                
                # Process keyword filter - normalize keywords
                keywords = []
                if keyword_filter:
                    keyword_filter = keyword_filter.strip()
                    if keyword_filter:
                        # Split keywords by comma and normalize them
                        keywords = [kw.strip().lower() for kw in keyword_filter.split(',') if kw.strip()]
                
                # Check each message for matching criteria
                for mail_id in reversed(mail_ids):  # Check from newest to oldest
                    try:
                        # Fetch header for efficiency
                        status, header_data = self.connection.fetch(mail_id, '(BODY[HEADER.FIELDS (FROM SUBJECT)])')
                        
                        if status == 'OK' and header_data[0]:
                            header_text = header_data[0][1].decode('utf-8', errors='ignore')
                            
                            # Extract headers
                            from_header = ''
                            subject_header = ''
                            for line in header_text.split('\n'):
                                line_lower = line.lower()
                                if line_lower.startswith('from:'):
                                    from_header = line[5:].strip()
                                elif line_lower.startswith('subject:'):
                                    subject_header = line[8:].strip()
                            
                            # Check sender filter (if enabled)
                            sender_match = True
                            if normalized_senders:
                                sender_match = False
                                if from_header:
                                    _, sender_email = self._parse_address(from_header)
                                    sender_email_lower = sender_email.lower()
                                    sender_match = any(allowed_sender in sender_email_lower or sender_email_lower == allowed_sender 
                                                     for allowed_sender in normalized_senders)
                            
                            # Check keyword filter (if enabled)
                            keyword_match = True
                            if keywords:
                                keyword_match = False
                                if subject_header:
                                    # Decode subject header if necessary
                                    decoded_subject = self._decode_header(subject_header).lower()
                                    keyword_match = any(keyword in decoded_subject for keyword in keywords)
                            
                            # Add to filtered list if both conditions match
                            if sender_match and keyword_match:
                                filtered_mail_ids.append(mail_id)
                                        
                    except Exception as e:
                        logger.warning(f"Error checking filters for mail {mail_id}: {e}")
                        continue
                
                if not filtered_mail_ids:
                    # Create appropriate message based on active filters
                    filter_messages = []
                    if normalized_senders:
                        filter_messages.append(f"发件人: {', '.join(sender_filter)}")
                    if keywords:
                        filter_messages.append(f"关键词: {keyword_filter}")
                    
                    filter_description = '; '.join(filter_messages)
                    return {
                        'success': True,
                        'message': f'邮箱中没有符合过滤条件的邮件 (过滤条件: {filter_description})',
                        'mail': None
                    }
                
                # Use filtered mail IDs (already sorted newest first)
                available_mail_ids = filtered_mail_ids
            else:
                # No filters, use all mail IDs in reverse order (newest first)
                available_mail_ids = list(reversed(mail_ids))
            
            # Check if requested index is within range
            if email_index >= len(available_mail_ids):
                return {
                    'success': True,
                    'message': '没有更多邮件',
                    'mail': None
                }
            
            # Determine which emails to fetch based on index and limit
            if email_limit == 1:
                # Single email mode (backward compatibility)
                target_id = available_mail_ids[email_index]
                
                # Fetch the selected message data
                status, msg_data = self.connection.fetch(target_id, '(RFC822)')
                
                if status != 'OK':
                    raise Exception("无法获取邮件内容")
                
                # Parse the email
                raw_email = msg_data[0][1]
                email_message = email.message_from_bytes(raw_email)
                
                # Extract email information
                mail_info = self._parse_email(email_message)
                
                # Add filtering information to the response
                filter_info = []
                if days_filter:
                    filter_info.append(f"最近{days_filter}天内")
                if sender_filter:
                    filter_info.append(f"发件人: {', '.join(sender_filter)}")
                if keyword_filter:
                    filter_info.append(f"关键词: {keyword_filter}")
                
                if filter_info:
                    mail_info['filter_applied'] = '; '.join(filter_info)
                
                mail_info['folder'] = normalize_folder_name(selected_folder)
                
                return {
                    'success': True,
                    'mail': mail_info
                }
            else:
                # Multiple emails mode
                emails = []
                end_index = min(email_index + email_limit, len(available_mail_ids))
                
                for i in range(email_index, end_index):
                    try:
                        target_id = available_mail_ids[i]
                        
                        # Fetch the message data
                        status, msg_data = self.connection.fetch(target_id, '(RFC822)')
                        
                        if status != 'OK':
                            continue
                        
                        # Parse the email
                        raw_email = msg_data[0][1]
                        email_message = email.message_from_bytes(raw_email)
                        
                        # Extract email information
                        mail_info = self._parse_email(email_message)
                        mail_info['folder'] = normalize_folder_name(selected_folder)
                        emails.append(mail_info)
                        
                    except Exception as e:
                        logger.warning(f"Error fetching email {i}: {e}")
                        continue
                
                return {
                    'success': True,
                    'mails': emails,
                    'total_count': len(available_mail_ids),
                    'fetched_count': len(emails)
                }
            
        except Exception as e:
            return {
                'success': False,
                'message': f'获取邮件失败: {str(e)}'
            }
            
    def get_latest_mail(self):
        """Get the latest email from the mailbox (legacy method, calls filtered version)"""
        return self.get_latest_mail_filtered()
            
    def _parse_email(self, email_message):
        """Parse email message and extract information"""
        # Decode subject
        subject = self._decode_header(email_message.get('Subject', ''))
        
        # Get sender info
        from_header = email_message.get('From', '')
        from_name, from_email = self._parse_address(from_header)
        
        # Create proper sender display format: "Name <email@domain.com>"
        if from_name and from_email and from_name != from_email:
            # If we have both a name and email address, format as "Name <email>"
            sender_display = f"{from_name} <{from_email}>"
        elif from_email and from_email != '未知':
            # If we only have email, use just the email
            sender_display = from_email
        else:
            # Fallback to name or unknown
            sender_display = from_name or '未知'
        
        # Get recipient info
        to_header = email_message.get('To', '')
        to_name, to_email = self._parse_address(to_header)
        
        # Get date
        date_header = email_message.get('Date', '')
        formatted_date = self._parse_date(date_header)
        
        # Get message ID
        message_id = email_message.get('Message-ID', '')
        
        # Parse body and attachments
        body_info = self._parse_body(email_message)
        
        mail_data = {
            'subject': subject,
            'from': sender_display,
            'from_email': from_email,
            'to': to_email,
            'date': formatted_date,
            'message_id': message_id
        }
        
        # Add body information
        mail_data.update(body_info)
        
        return mail_data
        
    def _decode_header(self, header_value):
        """Decode email header"""
        if not header_value:
            return ''
            
        decoded_parts = decode_header(header_value)
        decoded_string = ''
        
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                if encoding:
                    try:
                        decoded_string += part.decode(encoding)
                    except:
                        decoded_string += part.decode('utf-8', errors='ignore')
                else:
                    decoded_string += part.decode('utf-8', errors='ignore')
            else:
                decoded_string += part
                
        return decoded_string
        
    def _parse_address(self, address_header):
        """Parse email address from header"""
        if not address_header:
            return '未知', '未知'
            
        try:
            # Simple parsing - can be enhanced for more complex cases
            address_header = address_header.strip()
            
            if '<' in address_header and '>' in address_header:
                # Format: Name <email@domain.com>
                name_part = address_header.split('<')[0].strip().strip('"')
                email_part = address_header.split('<')[1].split('>')[0].strip()
                return self._decode_header(name_part) or email_part, email_part
            elif '(' in address_header and ')' in address_header:
                # Format: email@domain.com (Name)
                email_part = address_header.split('(')[0].strip()
                name_part = address_header.split('(')[1].split(')')[0].strip()
                return name_part or email_part, email_part
            else:
                # Simple format: just email
                return address_header.strip(), address_header.strip()
        except:
            return address_header, address_header
            
    def _parse_date(self, date_header):
        """Parse email date"""
        if not date_header:
            return '未知'
            
        try:
            # Convert to standard format
            from email.utils import parsedate_tz, mktime_tz
            date_tuple = parsedate_tz(date_header)
            if date_tuple:
                timestamp = mktime_tz(date_tuple)
                return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
        except:
            pass
            
        return date_header
        
    def _parse_body(self, email_message):
        """Parse email body and attachments"""
        body_text = ''
        body_html = ''
        images = []
        attachments = []
        
        if email_message.is_multipart():
            for part in email_message.walk():
                result = self._process_part(part)
                if result['type'] == 'text' and not body_text:
                    body_text = result['content']
                elif result['type'] == 'html' and not body_html:
                    body_html = result['content']
                elif result['type'] == 'image':
                    if result['disposition'] == 'attachment':
                        attachments.append(result['data'])
                    else:
                        images.append(result['data'])
                elif result['type'] == 'attachment':
                    attachments.append(result['data'])
        else:
            result = self._process_simple_part(email_message)
            body_text = result[0]
            body_html = result[1]
            
        # Determine main content
        main_content = body_html or body_text
        content_type = 'html' if body_html else 'text'
        
        if not main_content and images:
            content_type = 'image'
            
        return {
            'body_type': content_type,
            'body': main_content or '无法读取邮件内容',
            'images': images,
            'attachments': attachments
        }
        
    def _process_part(self, part):
        """Process individual email part and return result"""
        content_type = part.get_content_type()
        content_disposition = part.get('Content-Disposition', '')
        
        if content_type == 'text/plain' and 'attachment' not in content_disposition:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or 'utf-8'
                try:
                    content = payload.decode(charset, errors='ignore')
                except:
                    content = payload.decode('utf-8', errors='ignore')
                return {'type': 'text', 'content': content}
                    
        elif content_type == 'text/html' and 'attachment' not in content_disposition:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or 'utf-8'
                try:
                    content = payload.decode(charset, errors='ignore')
                except:
                    content = payload.decode('utf-8', errors='ignore')
                return {'type': 'html', 'content': content}
                    
        elif content_type.startswith('image/'):
            filename = part.get_filename() or f'image.{content_type.split("/")[1]}'
            payload = part.get_payload(decode=True)
            if payload:
                image_info = {
                    'filename': filename,
                    'mime_type': content_type,
                    'content': base64.b64encode(payload).decode('ascii')
                }
                
                disposition = 'attachment' if 'attachment' in content_disposition else 'inline'
                return {'type': 'image', 'data': image_info, 'disposition': disposition}
                    
        else:
            # Other attachments
            filename = part.get_filename()
            if filename or 'attachment' in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    attachment_info = {
                        'filename': filename or 'attachment',
                        'mime_type': content_type,
                        'content': base64.b64encode(payload).decode('ascii')
                    }
                    return {'type': 'attachment', 'data': attachment_info}
                    
        return {'type': 'none'}
    
    def _process_simple_part(self, part):
        """Process simple non-multipart email"""
        body_text = ''
        body_html = ''
        
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True)
        
        if payload:
            charset = part.get_content_charset() or 'utf-8'
            try:
                content = payload.decode(charset, errors='ignore')
            except:
                content = payload.decode('utf-8', errors='ignore')
                
            if content_type == 'text/plain':
                body_text = content
            elif content_type == 'text/html':
                body_html = content
            else:
                body_text = content  # Fallback to text
                
        return body_text, body_html
                    
    def close(self):
        """Close connection"""
        if self.connection:
            try:
                self.connection.close()
                self.connection.logout()
            except:
                pass
            self.connection = None
            
    def test_connection(self):
        """Test connection and provide diagnostics"""
        diagnostics = {
            'server_info': f'{self.server}:{self.port}',
            'protocol_info': f'{self.protocol.upper()} with {"SSL" if self.use_ssl else "no SSL"}',
            'proxy_status': 'Disabled'
        }
        
        if self.proxy_enabled:
            diagnostics['proxy_status'] = f"Enabled - {self.proxy_info['type']} ({self.proxy_info['name']})"
            diagnostics['proxy_info'] = f"{self.proxy_info['host']}:{self.proxy_info['port']}"
        
        connection_method = "直连"
        if self.proxy_enabled:
            connection_method = f"代理 ({self.proxy_info['type']} - {self.proxy_info['name']})"
        
        try:
            logger.info(f"Starting connection test via {connection_method}...")
            
            # Test DNS resolution first (only for direct connections)
            if not self.proxy_enabled:
                try:
                    import socket
                    resolved_ip = socket.gethostbyname(self.server)
                    diagnostics['dns_resolution'] = f'✅ 服务器地址解析成功 (IP: {resolved_ip})'
                except Exception as e:
                    diagnostics['dns_resolution'] = f'❌ DNS解析失败: {str(e)}'
                    return {
                        'success': False,
                        'message': f'❌ DNS解析失败: {str(e)} ({connection_method})',
                        'diagnostics': diagnostics,
                        'error_type': 'dns_error'
                    }
            else:
                diagnostics['dns_resolution'] = '⚠️ 通过代理连接，跳过DNS测试'
            
            # Test basic TCP connection (only for direct connections)
            if not self.proxy_enabled:
                try:
                    test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    test_socket.settimeout(10)
                    result = test_socket.connect_ex((self.server, self.port))
                    test_socket.close()
                    
                    if result == 0:
                        diagnostics['tcp_connection'] = '✅ TCP连接测试成功'
                    else:
                        diagnostics['tcp_connection'] = f'❌ TCP连接失败 (错误码: {result})'
                except Exception as e:
                    diagnostics['tcp_connection'] = f'❌ TCP连接测试失败: {str(e)}'
            else:
                diagnostics['tcp_connection'] = '⚠️ 通过代理连接，跳过TCP测试'
            
            # Test full connection
            connection_success = False
            try:
                connection_success = self.connect()
                if connection_success:
                    self.close()
                    diagnostics['connection_test'] = f'✅ 邮箱连接成功 (通过{connection_method})'
                    diagnostics['auth_status'] = '✅ 身份验证成功'
                    diagnostics['mailbox_access'] = '✅ 邮箱访问正常'
                    
                    return {
                        'success': True,
                        'message': f'✅ 邮箱连接测试成功！(通过{connection_method})',
                        'diagnostics': diagnostics
                    }
                else:
                    diagnostics['connection_test'] = f'❌ 邮箱连接失败 (通过{connection_method})'
            except Exception as e:
                error_message = str(e)
                diagnostics['connection_test'] = f'❌ 连接失败: {error_message}'
                
                # Categorize error types for better user feedback
                error_type = 'unknown'
                if 'ssl' in error_message.lower() or 'certificate' in error_message.lower() or 'handshake' in error_message.lower():
                    error_type = 'ssl_error'
                    diagnostics['ssl_status'] = '❌ SSL连接失败'
                elif 'authentication' in error_message.lower() or 'login' in error_message.lower() or 'credentials' in error_message.lower() or '用户名或密码' in error_message:
                    error_type = 'auth_failed'
                    diagnostics['auth_status'] = '❌ 身份验证失败'
                elif 'connection' in error_message.lower() or 'refused' in error_message.lower() or '连接被拒绝' in error_message:
                    error_type = 'connection_refused'
                    diagnostics['connection_status'] = '❌ 连接被拒绝'
                elif 'timeout' in error_message.lower() or '超时' in error_message:
                    error_type = 'timeout'
                    diagnostics['connection_status'] = '❌ 连接超时'
                elif 'proxy' in error_message.lower() or '代理' in error_message:
                    error_type = 'proxy_error'
                    diagnostics['proxy_status'] = '❌ 代理连接失败'
                elif 'dns' in error_message.lower() or '解析' in error_message:
                    error_type = 'dns_error'
                    diagnostics['dns_resolution'] = f'❌ {error_message}'
                else:
                    diagnostics['error_details'] = error_message
                    
                return {
                    'success': False,
                    'message': f'❌ 连接测试失败: {error_message}',
                    'diagnostics': diagnostics,
                    'error_type': error_type
                }
            
            return {
                'success': False,
                'message': f'❌ 邮箱连接测试失败 (通过{connection_method})',
                'diagnostics': diagnostics,
                'error_type': 'connection_failed'
            }
                
        except Exception as e:
            error_message = str(e)
            logger.error(f"Connection test exception: {error_message}")
            
            diagnostics['exception_error'] = f'❌ 测试过程异常: {error_message}'
            
            return {
                'success': False,
                'message': f'❌ 连接测试异常: {error_message}',
                'diagnostics': diagnostics,
                'error_type': 'test_exception'
            }


def main():
    """Main function for CLI usage"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Email Fetcher with Card Filtering')
    parser.add_argument('email', help='Email address to fetch from')
    parser.add_argument('--test-connection', action='store_true', help='Test connection only')
    parser.add_argument('--days-filter', type=int, default=None, help='Filter emails within X days')
    parser.add_argument('--sender-filter', type=str, default='', help='Filter by sender email addresses (comma separated)')
    parser.add_argument('--keyword-filter', type=str, default='', help='Filter by subject keywords (comma separated)')
    parser.add_argument('--card-key', type=str, default='', help='Card key for usage tracking')
    parser.add_argument('--admin-access', action='store_true', help='Admin access mode (bypass card validation)')
    parser.add_argument('--index', type=int, default=0, help='Email index to fetch (0=latest, 1=second latest, etc.)')
    parser.add_argument('--limit', type=int, default=1, help='Number of emails to fetch')
    parser.add_argument('--folder', type=str, default='INBOX', help='Mailbox folder to fetch from')
    
    try:
        args = parser.parse_args()
    except:
        # Fallback to old argument parsing for compatibility
        if len(sys.argv) < 2:
            print("Usage: python mail_fetcher.py <email> [--test-connection]")
            sys.exit(1)
            
        email_address = sys.argv[1]
        test_mode = len(sys.argv) > 2 and sys.argv[2] == '--test-connection'
        days_filter = None
        sender_filter = ''
        keyword_filter = ''
        card_key = ''
        email_index = 0
        email_limit = 1
        folder = 'INBOX'
    else:
        email_address = args.email
        test_mode = args.test_connection
        days_filter = args.days_filter
        sender_filter = args.sender_filter
        keyword_filter = args.keyword_filter
        card_key = args.card_key
        email_index = args.index
        email_limit = args.limit
        folder = args.folder or 'INBOX'
    
    try:
        # Get email account from database
        db_path = os.path.join(os.path.dirname(__file__), '..', 'db', 'mail.sqlite')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM mail_accounts WHERE email = ?', (email_address,))
        account = cursor.fetchone()
        
        if not account:
            # Even when mailbox doesn't exist, we need to check proxy status for proper error message display
            fetcher = ProxyMailFetcher('dummy', 0, 'dummy', 'dummy')  # Create minimal instance just for proxy check
            proxy_info = fetcher.get_proxy_info()
            
            # Construct error message based on connection type
            if proxy_info['enabled']:
                error_message = '邮箱账号不存在，请联系管理员添加 (代理)'
            else:
                error_message = '邮箱账号不存在，请联系管理员添加 (直连)'
            
            result = {
                'success': False,
                'message': error_message,
                'proxy': proxy_info
            }
        else:
            # Map database columns
            columns = [desc[0] for desc in cursor.description]
            account_dict = dict(zip(columns, account))
            
            # Create fetcher instance
            fetcher = ProxyMailFetcher(
                account_dict['server'],
                account_dict['port'],
                account_dict['username'],
                account_dict['password'],
                account_dict['protocol'],
                bool(account_dict['ssl'])
            )
            
            if test_mode:
                # Run connection test
                result = fetcher.test_connection()
                proxy_info = fetcher.get_proxy_info()
                result['proxy'] = proxy_info
            else:
                # Connect and get emails with filtering
                if fetcher.connect():
                    # Apply filtering parameters if provided
                    filter_params = {}
                    if days_filter is not None:
                        filter_params['days_filter'] = days_filter
                    if sender_filter:
                        filter_params['sender_filter'] = sender_filter.split(',')
                    if keyword_filter:
                        filter_params['keyword_filter'] = keyword_filter
                    if email_index > 0:
                        filter_params['index'] = email_index
                    if email_limit > 1:
                        filter_params['limit'] = email_limit
                    if folder:
                        filter_params['folder'] = folder
                    
                    result = fetcher.get_latest_mail_filtered(filter_params)
                    proxy_info = fetcher.get_proxy_info()
                    result['proxy'] = proxy_info
                    fetcher.close()
                else:
                    proxy_info = fetcher.get_proxy_info()
                    result = {
                        'success': False,
                        'message': '无法连接到邮件服务器，请检查邮箱配置',
                        'proxy': proxy_info
                    }
        
        conn.close()
        
        # Output JSON result
        print(json.dumps(result, ensure_ascii=False))
        
    except Exception as e:
        try:
            # Try to get proxy info even for general errors
            fetcher = ProxyMailFetcher('dummy', 0, 'dummy', 'dummy')
            proxy_info = fetcher.get_proxy_info()
            result = {
                'success': False,
                'message': f'服务器错误: {str(e)}',
                'proxy': proxy_info
            }
        except:
            # Fallback if proxy info can't be obtained
            result = {
                'success': False,
                'message': f'服务器错误: {str(e)}'
            }
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
