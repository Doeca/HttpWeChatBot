import asyncio
import contextlib
import json
from typing import Any, AsyncGenerator, Callable, Generator, Optional, Type, cast

import msgpack

from wechatbot_client.config import Config
from wechatbot_client.consts import RECONNECT_INTERVAL
from wechatbot_client.driver import (
    URL,
    Driver,
    ForwardWebSocket,
    HTTPServerSetup,
    Request,
    Response,
    WebSocket,
    WebSocketServerSetup,
)
from wechatbot_client.exception import WebSocketClosed
from wechatbot_client.log import logger
from wechatbot_client.onebot12.event import Event
from wechatbot_client.utils import escape_tag

from .api_manager import ApiManager
from .utils import flattened_to_nested, get_auth_bearer, log


class WeChatManager:
    """
    微信客户端管理
    """

    config: Config
    """应用设置"""
    api_manager: ApiManager
    """api管理模块"""
    self_id: str
    """自身微信id"""
    driver: Driver
    """后端驱动"""
    event_models: dict
    """事件模型映射"""
    tasks: list[asyncio.Task]
    """正向连接ws任务列表"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.driver = Driver(config)
        self.api_manager = ApiManager()
        self.self_id = None
        self.task = []

    def init(self) -> None:
        """
        初始化wechat管理端
        """
        self.api_manager.init()

        logger.debug("<y>开始获取wxid...</y>")
        self.self_id = self.api_manager.get_wxid()
        logger.debug("<g>微信id获取成功...</g>")
        logger.info("<g>初始化完成，启动uvicorn...</g>")

    def open_recv_msg(self, file_path: str) -> None:
        """
        开始接收消息
        """
        self.api_manager.open_recv_msg(file_path)

    def close(self) -> None:
        """
        管理微信管理模块
        """
        self.api_manager.close()

    def register_message_handler(self, func: Callable[[str], None]) -> None:
        """
        注册一个消息处理器
        """
        self.api_manager.register_message_handler(func)

    def setup_http_server(self, setup: HTTPServerSetup) -> None:
        """设置一个 HTTP 服务器路由配置"""
        self.driver.setup_http_server(setup)

    def setup_websocket_server(self, setup: WebSocketServerSetup) -> None:
        """设置一个 WebSocket 服务器路由配置"""
        self.driver.setup_websocket_server(setup)

    async def request(self, setup: Request) -> Response:
        """进行一个 HTTP 客户端请求"""
        return await self.driver.request(setup)

    @contextlib.asynccontextmanager
    async def websocket(self, setup: Request) -> AsyncGenerator[ForwardWebSocket, None]:
        """建立一个 WebSocket 客户端连接请求"""
        async with self.driver.websocket(setup) as ws:
            yield ws

    def _check_access_token(self, request: Request) -> Optional[Response]:
        """
        检测access_token
        """
        token = get_auth_bearer(request.headers.get("Authorization"))

        access_token = self.config.onebot_access_token
        if access_token and access_token != token:
            msg = (
                "Authorization Header is invalid"
                if token
                else "Missing Authorization Header"
            )
            log("WARNING", msg)
            return Response(403, content=msg)

    async def _handle_ws(self, websocket: WebSocket) -> None:
        """
        当有新的ws连接时的任务
        """

        # check access_token
        response = self._check_access_token(websocket.request)
        if response is not None:
            content = cast(str, response.content)
            await websocket.close(1008, content)
            return

        # 后续处理代码
        seq = self.driver.ws_connect(websocket)
        log("SUCCESS", f"新的websocket连接，编号为: {seq}...")

        try:
            while True:
                data = await websocket.receive()
                raw_data = (
                    json.loads(data) if isinstance(data, str) else msgpack.unpackb(data)
                )
                if event := self.json_to_event(raw_data):
                    asyncio.create_task(self.handle_event(event))
        except WebSocketClosed:
            log(
                "WARNING",
                f"编号为: {seq} 的websocket被远程关闭了...",
            )
        except Exception as e:
            log(
                "ERROR",
                "<r><bg #f8bbd0>处理来自 websocket 的数据时出错 "
                f"- 编号: {seq}.</bg #f8bbd0></r>",
                e,
            )

        finally:
            with contextlib.suppress(Exception):
                await websocket.close()
            self.driver.ws_disconnect(seq)

    async def _start_forward(self) -> None:
        """
        开启正向ws连接
        """
        for url in self.config.onebot_ws_urls:
            try:
                ws_url = URL(url)
                self.tasks.append(asyncio.create_task(self._forward_ws(ws_url)))
            except Exception as e:
                log(
                    "ERROR",
                    f"<r><bg #f8bbd0>Bad url {escape_tag(url)} "
                    "in onebot forward websocket config</bg #f8bbd0></r>",
                    e,
                )

    async def _forward_ws(self, url: URL) -> None:
        """
        正向连接ws任务
        """
        headers = {}
        if self.config.onebot_access_token:
            headers["Authorization"] = f"Bearer {self.config.onebot_access_token}"
        req = Request("GET", url, headers=headers, timeout=30.0)
        while True:
            try:
                async with self.websocket(req) as ws:
                    log(
                        "DEBUG",
                        f"WebSocket Connection to {escape_tag(str(url))} established",
                    )
                    seq = self.driver.ws_connect(ws)
                    log("SUCCESS", f"<y>新的websocket连接，编号为: {seq}...")
                    try:
                        while True:
                            data = await ws.receive()
                            raw_data = (
                                json.loads(data)
                                if isinstance(data, str)
                                else msgpack.unpackb(data)
                            )
                            event = self.json_to_event(raw_data)
                            if not event:
                                continue
                            # asyncio.create_task(handle_event(event))
                    except WebSocketClosed as e:
                        log(
                            "ERROR",
                            "<r><bg #f8bbd0>WebSocket 关闭了...</bg #f8bbd0></r>",
                            e,
                        )
                    except Exception as e:
                        log(
                            "ERROR",
                            "<r><bg #f8bbd0>处理来自 websocket 的数据时出错"
                            f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                            e,
                        )
                    finally:
                        self.driver.ws_disconnect(seq)

            except Exception as e:
                log(
                    "ERROR",
                    "<r><bg #f8bbd0>Error while setup websocket to "
                    f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                    e,
                )

            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _stop_forward(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()

    @classmethod
    def get_event_model(
        cls, data: dict[str, Any]
    ) -> Generator[Type[Event], None, None]:
        """根据事件获取对应 `Event Model` 及 `FallBack Event Model` 列表。"""
        key = f"/{data.get('impl')}/{data.get('platform')}"
        if key in cls.event_models:
            yield from cls.event_models[key].get_model(data)
        yield from cls.event_models[""].get_model(data)

    @classmethod
    def json_to_event(cls, json_data: Any) -> Optional[Event]:
        """
        反序列化event
        """
        if not isinstance(json_data, dict):
            return None

        # transform flattened dict to nested
        json_data = flattened_to_nested(json_data)

        try:
            for model in cls.get_event_model(json_data):
                try:
                    event = model.parse_obj(json_data)
                    break
                except Exception as e:
                    log("DEBUG", "Event Parse Error", e)
            else:
                event = Event.parse_obj(json_data)
            return event

        except Exception as e:
            log(
                "ERROR",
                "<r><bg #f8bbd0>Failed to parse event. "
                f"Raw: {str(json_data)}</bg #f8bbd0></r>",
                e,
            )
            return None
