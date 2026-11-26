#!/usr/bin/env python3
"""常见入口别名；直接运行时启动零依赖 HTTP 服务。"""

from whiteboard.server import create_server, main

__all__ = ["create_server", "main"]

app = None  # 本服务使用标准库 HTTPServer，可用 create_server() 取得 server/service。

if __name__ == "__main__":
    main()
