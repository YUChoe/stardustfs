#!/usr/bin/env python3
"""StardustFS - WebDAV 기반 암호화 가상 파일시스템."""

import argparse
import logging
import sys

from stardustlib.initializer import initialize_system


def main() -> None:
    """메인 엔트리포인트. --config 인자를 처리하고 cheroot 서버를 시작한다."""
    parser = argparse.ArgumentParser(description="StardustFS WebDAV Server")
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="JSON 설정 파일 경로",
    )
    args = parser.parse_args()

    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 시스템 초기화
    app, config = initialize_system(args.config)

    # cheroot 서버 시작
    from cheroot.wsgi import Server as WSGIServer

    host = config["webdav"]["host"]  # 항상 "127.0.0.1" (ConfigLoader가 강제)
    port = config["webdav"]["port"]

    server = WSGIServer((host, port), app)
    logging.info("WebDAV 서버 시작: http://%s:%d/", host, port)

    try:
        server.start()
    except KeyboardInterrupt:
        logging.info("서버 종료 중...")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
