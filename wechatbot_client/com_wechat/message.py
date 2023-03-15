"""
这里将comwechat收到的message解析为event
"""
import re
from inspect import iscoroutinefunction
from pathlib import Path
from typing import Callable, Generic, Optional, TypeVar
from urllib.parse import unquote
from uuid import uuid4
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element

from wechatbot_client.file_manager import FileManager
from wechatbot_client.onebot12 import Message, MessageSegment
from wechatbot_client.onebot12.event import (
    BotSelf,
    Event,
    FriendRequestEvent,
    GetGroupAnnouncementNotice,
    GetGroupFileNotice,
    GetGroupPokeNotice,
    GetGroupRedBagNotice,
    GetPrivateFileNotice,
    GetPrivatePokeNotice,
    GetPrivateRedBagNotice,
    GroupMessageDeleteEvent,
    GroupMessageEvent,
    PrivateMessageDeleteEvent,
    PrivateMessageEvent,
)

from .model import Message as WechatMessage
from .type import AppType, WxType

E = TypeVar("E", bound=Event)

HANDLE_DICT: dict[int, Callable[[WechatMessage], E]] = {}
"""消息处理器字典"""
APP_HANDLERS: dict[
    int, Callable[["MessageHandler", WechatMessage, Element], Optional[E]]
] = {}
"""app消息处理函数列表"""
SYS_HANDLERS: list[Callable[[WechatMessage], Optional[E]]] = []
"""系统消息处理函数列表"""
GROUP_SYS_HANDLERS: list[Callable[[WechatMessage, Element], Optional[E]]] = []
"""群系统消息处理函数列表"""


def add_handler(_tpye: int) -> Callable[[WechatMessage], E]:
    """
    添加消息处理器
    """

    def _handle(func: Callable[[WechatMessage], E]) -> Callable[[WechatMessage], E]:
        global HANDLE_DICT
        HANDLE_DICT[_tpye] = func
        return func

    return _handle


def add_app_handler(_tpye: int) -> Callable[[WechatMessage], Optional[E]]:
    """
    添加app_handler
    """

    def _handle(
        func: Callable[[WechatMessage, Element], Optional[E]]
    ) -> Callable[[WechatMessage, Element], Optional[E]]:
        global APP_HANDLERS
        APP_HANDLERS[_tpye] = func
        return func

    return _handle


def add_sys_handler(
    func: Callable[[WechatMessage], Optional[E]]
) -> Callable[[WechatMessage], Optional[E]]:
    """添加系统处理器"""
    global SYS_HANDLERS
    SYS_HANDLERS.append(func)
    return func


def add_group_sys_handler(
    func: Callable[[WechatMessage, Element], Optional[E]]
) -> Callable[[WechatMessage], Optional[E]]:
    """添加系统处理器"""
    global GROUP_SYS_HANDLERS
    GROUP_SYS_HANDLERS.append(func)
    return func


class MessageHandler(Generic[E]):
    """
    微信消息处理器
    """

    image_path: Path
    """图片文件路径"""
    voice_path: Path
    """语音文件路径"""
    wechat_path: Path
    """微信文件路径"""
    file_manager: FileManager
    """文件处理器"""

    def __init__(
        self,
        image_path: Path,
        voice_path: Path,
        wechat_path: Path,
        file_manager: FileManager,
    ) -> None:
        self.image_path = image_path
        self.voice_path = voice_path
        self.wechat_path = wechat_path
        self.file_manager = file_manager

    async def handle_message(self, msg: WechatMessage) -> Optional[E]:
        """
        处理消息，返回事件
        """
        _type = msg.type
        handler = HANDLE_DICT.get(_type)
        if handler is None:
            return None
        if iscoroutinefunction(handler):
            result = await handler(msg)
        else:
            result = handler(msg)
        return result

    def _find_file(file_path: str) -> Optional[Path]:
        """
        找个图片
        """
        jpg = Path(f"{file_path}.jpg")
        if jpg.exists():
            return jpg
        png = Path(f"{file_path}.png")
        if png.exists():
            return png
        gif = Path(f"{file_path}.gif")
        if gif.exists():
            return gif
        return None

    @add_handler(WxType.TEXT_MSG)
    def handle_text(self, msg: WechatMessage) -> E:
        """
        处理文本
        """
        # 获取at
        raw_xml = msg.extrainfo
        xml_obj = ET.fromstring(raw_xml)
        at_xml = xml_obj.find("./atuserlist")
        event_id = str(uuid4())
        if at_xml is None:
            # 没有at
            # 获取message
            message = Message(MessageSegment.text(msg.message))
            # 判断群聊还是私聊
            if "@chatroom" in msg.sender:
                return GroupMessageEvent(
                    id=event_id,
                    time=msg.timestamp,
                    self=BotSelf(user_id=msg.self),
                    message_id=str(msg.msgid),
                    message=message,
                    alt_message=str(message),
                    user_id=msg.wxid,
                    group_id=msg.sender,
                )
            return PrivateMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
            )

        # 获取at
        at_list = at_xml.text.split(",")
        if at_list[0] == "":
            # pc微信发消息at时，会多一个','
            at_list.pop(0)

        # 这里用正则分割文本，来制造消息段，可能会有bug
        regex = r"(@[^@\s]+\s)"
        msg_list = re.split(regex, msg.message)
        new_msg = Message()
        for index, one_msg in enumerate(msg_list):
            if re.search(regex, one_msg) is None:
                new_msg.append(MessageSegment.text(one_msg))
            else:
                try:
                    at_one = at_list.pop(0)
                except IndexError:
                    # 这里已经没有at目标了
                    text = "".join(msg_list[index:])
                    new_msg.append(MessageSegment.text(text))
                    break
                if at_one == "notify@all":
                    new_msg.append(MessageSegment.mention_all())
                else:
                    new_msg.append(MessageSegment.mention(at_one))
        return GroupMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=new_msg,
            alt_message=str(message),
            user_id=msg.wxid,
            group_id=msg.sender,
        )

    @add_handler(WxType.IMAGE_MSG)
    async def handle_image(self, msg: WechatMessage) -> E:
        """
        处理图片
        """
        event_id = str(uuid4())
        file_name = Path(msg.filepath).stem
        # 找图片
        file_path = f"{self.image_path.absolute()}/{file_name}"
        file = self._find_file(file_path)
        if file is None:
            return None
        file_id = await self.file_manager.cache_file_id_from_path(file, file.name)
        message = Message(MessageSegment.image(file_id=file_id))
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @add_handler(WxType.VOICE_MSG)
    async def handle_voice(self, msg: WechatMessage) -> E:
        """
        处理语音
        """
        event_id = str(uuid4())
        file_name = msg.sign
        file = self.voice_path / f"{file_name}.amr"
        file_id = await self.file_manager.cache_file_id_from_path(file, file.name)
        message = Message(MessageSegment.image(file_id=file_id))
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @add_handler(WxType.FRIEND_REQUEST)
    def handle_friend_request(self, msg: WechatMessage) -> E:
        """
        处理好友请求
        """
        event_id = str(uuid4())
        raw_xml = msg.message
        xml_obj = ET.fromstring(raw_xml)
        attrib = xml_obj.attrib
        return FriendRequestEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            user_id=attrib["fromusername"],
            v3=attrib["encryptusername"],
            v4=attrib["ticket"],
            nickname=attrib["fromnickname"],
            content=attrib["content"],
            country=attrib["country"],
            province=attrib["province"],
            city=attrib["city"],
        )

    @add_handler(WxType.CARD_MSG)
    def handle_card(self, msg: WechatMessage) -> E:
        """
        处理名片消息
        """
        event_id = str(uuid4())
        raw_xml = msg.message
        xml_obj = ET.fromstring(raw_xml)
        attrib = xml_obj.attrib
        message = Message(
            MessageSegment.card(
                v3=attrib["username"],
                v4=attrib["antispamticket"],
                head_url=attrib["bigheadimgurl"],
                province=attrib["province"],
                city=attrib["city"],
                sex=attrib["sex"],
            )
        )
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @add_handler(WxType.VIDEO_MSG)
    async def handle_video(self, msg: WechatMessage) -> E:
        """
        处理视频
        """
        event_id = str(uuid4())
        video_img = self.wechat_path / msg.thumb_path
        video_path = video_img.parent
        video_name = f"{video_img.stem}.mp4"
        video = video_path / video_name
        file_id = await self.file_manager.cache_file_id_from_path(video, video_name)
        message = Message(MessageSegment.video(file_id=file_id))
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @add_handler(WxType.EMOJI_MSG)
    async def handle_emoji(self, msg: WechatMessage) -> E:
        """
        处理gif表情
        """
        event_id = str(uuid4())
        # 获取文件名
        raw_xml = msg.message
        xml_obj = ET.fromstring(raw_xml)
        emoji_url = xml_obj.find("./emoji").attrib.get("cdnurl")
        emoji = unquote(emoji_url)
        file_id = await self.file_manager.cache_file_id_from_url(
            emoji, f"{msg.msgid}.gif"
        )
        message = Message(MessageSegment.image(file_id=file_id))
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @add_handler(WxType.LOCATION_MSG)
    def handle_location(self, msg: WechatMessage) -> E:
        """
        处理位置信息
        """
        event_id = str(uuid4())
        raw_xml = msg.message
        xml_obj = ET.fromstring(raw_xml)
        attrib = xml_obj.attrib
        message = Message(
            MessageSegment.location(
                latitude=attrib["x"],
                longitude=attrib["y"],
                title=attrib["label"],
                content=attrib["poiname"],
            )
        )
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @add_handler(WxType.APP_MSG)
    async def handle_app(self, msg: WechatMessage) -> Optional[E]:
        """
        处理app消息
        """
        raw_xml = msg.message
        xml_obj = ET.fromstring(raw_xml)
        app = xml_obj.find("./appmsg")
        _type = int(app.find("./type").text)
        result = None
        handler = APP_HANDLERS.get(_type)
        if handler is None:
            return None
        if iscoroutinefunction(handler):
            result = await handler(self, msg, app)
        else:
            result = handler(self, msg, app)
        return result

    @add_handler(WxType.SYSTEM_MSG)
    def handle_sys(self, msg: WechatMessage) -> Optional[E]:
        """
        处理系统消息
        """
        result = None
        for handler in SYS_HANDLERS:
            result = handler(msg)
            if result is not None:
                break
        return result

    @add_handler(WxType.GROUP_SYS_MSG)
    def handle_group_sys(self, msg: WechatMessage) -> Optional[E]:
        """
        撤回消息事件
        """
        raw_xml = msg.message
        xml_obj = ET.fromstring(raw_xml)
        result = None
        for handler in GROUP_SYS_HANDLERS:
            result = handler(msg, xml_obj)
            if result is not None:
                break
        return result
        event_id = str(uuid4())
        raw_xml = msg.message
        xml_obj = ET.fromstring(raw_xml)
        msg_obj = xml_obj.find("./revokemsg/newmsgid")
        if msg_obj is None:
            return None
        message_id = msg_obj.text
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageDeleteEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=message_id,
                group_id=msg.sender,
                user_id=msg.wxid,
                operator_id="",
            )
        return PrivateMessageDeleteEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=message_id,
        )


class AppMessageHandler(Generic[E]):
    """
    app消息处理器
    """

    @classmethod
    @add_app_handler(AppType.LINK_MSG)
    def handle_link(
        cls, msg_handler: MessageHandler, msg: WechatMessage, app: Element
    ) -> E:
        """
        处理链接
        """
        event_id = str(uuid4())
        title = app.find("./title").text
        des = app.find("./des").text
        url = app.find("./url").text.replace(" ", "")
        image_path = msg.filepath
        if image_path != "":
            image_path = f"{msg_handler.wechat_path}/{image_path}"
        message = Message(
            MessageSegment.link(tittle=title, des=des, url=url, image=image_path)
        )
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @classmethod
    @add_app_handler(AppType.FILE_NOTICE)
    async def handle_file(
        cls, msg_handler: MessageHandler, msg: WechatMessage, app: Element
    ) -> E:
        """
        处理文件消息
        """
        event_id = str(uuid4())
        file_name = app.find("./title").text
        md5 = app.find("./md5").text
        appattach = app.find("./appattach")
        file_length = int(appattach.find("./totalen").text)
        # 判断是否为通知还是下载完成
        overwrite_newmsgid = appattach.find("./overwrite_newmsgid")
        if overwrite_newmsgid is None:
            # 通知事件
            # 检测是否为群聊
            if "@chatroom" in msg.sender:
                return GetGroupFileNotice(
                    id=event_id,
                    time=msg.timestamp,
                    self=BotSelf(user_id=msg.self),
                    message_id=msg.msgid,
                    user_id=msg.wxid,
                    group_id=msg.sender,
                    file_name=file_name,
                    file_length=file_length,
                    md5=md5,
                )
            else:
                return GetPrivateFileNotice(
                    id=event_id,
                    time=msg.timestamp,
                    self=BotSelf(user_id=msg.self),
                    message_id=msg.msgid,
                    user_id=msg.wxid,
                    file_name=file_name,
                    file_length=file_length,
                    md5=md5,
                )

        # 文件消息
        file_path = msg_handler.wechat_path / msg.filepath
        file_id = await msg_handler.file_manager.cache_file_id_from_path(
            file_path, file_name
        )
        message = Message(MessageSegment.file(file_id=file_id))
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @classmethod
    @add_app_handler(AppType.QUOTE)
    def handle_quote(
        cls, msg_handler: MessageHandler, msg: WechatMessage, app: Element
    ) -> E:
        """
        处理引用
        """
        event_id = str(uuid4())
        text = app.find("./title").text
        from_msgid = app.find("./refermsg/svrid").text
        from_user = app.find("./refermsg/fromusr").text
        message = MessageSegment.reply(
            message_id=from_msgid, user_id=from_user
        ) + MessageSegment.text(text)
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @classmethod
    @add_app_handler(AppType.APP)
    def handle_app(
        cls, msg_handler: MessageHandler, msg: WechatMessage, app: Element
    ) -> E:
        """
        处理app消息
        """
        event_id = str(uuid4())
        title = app.find("./title").text
        url = app.find("./url").text
        app_id = app.find("./weappinfo/username").text
        message = Message(MessageSegment.app(app_id, title, url))
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GroupMessageEvent(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                message_id=str(msg.msgid),
                message=message,
                alt_message=str(message),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return PrivateMessageEvent(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            message_id=str(msg.msgid),
            message=message,
            alt_message=str(message),
            user_id=msg.wxid,
        )

    @classmethod
    @add_app_handler(AppType.GROUP_ANNOUNCEMENT)
    def handle_announcement(
        cls, msg_handler: MessageHandler, msg: WechatMessage, app: Element
    ) -> E:
        """处理群公告"""
        event_id = str(uuid4())
        text = app.find("textannouncement").text
        return GetGroupAnnouncementNotice(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            group_id=msg.sender,
            user_id=msg.wxid,
            text=text,
        )

    @classmethod
    @add_app_handler(AppType.TRANSFER)
    def handle_transfer(
        cls, msg_handler: MessageHandler, msg: WechatMessage, app: Element
    ) -> E:
        """
        处理转账消息
        """
        # 告辞
        return None


class SysMessageHandler(Generic[E]):
    """
    系统消息处理器
    """

    @classmethod
    @add_sys_handler
    def read_bag(cls, msg: WechatMessage) -> Optional[E]:
        """"""
        if msg.message != "收到红包，请在手机上查看":
            return None
        event_id = str(uuid4())
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GetGroupRedBagNotice(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return GetPrivateRedBagNotice(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            user_id=msg.wxid,
        )

    @classmethod
    @add_sys_handler
    def poke(cls, msg: WechatMessage) -> Optional[E]:
        """
        拍一拍
        """
        if " 拍了拍我" not in msg.message:
            return None
        event_id = str(uuid4())
        # 检测是否为群聊
        if "@chatroom" in msg.sender:
            return GetGroupPokeNotice(
                id=event_id,
                time=msg.timestamp,
                self=BotSelf(user_id=msg.self),
                user_id=msg.wxid,
                group_id=msg.sender,
            )
        return GetPrivatePokeNotice(
            id=event_id,
            time=msg.timestamp,
            self=BotSelf(user_id=msg.self),
            user_id=msg.wxid,
        )


class GroupSysMsgHandler(Generic[E]):
    """
    群系统消息处理器
    """

    @classmethod
    @add_group_sys_handler
    def event(cls, msg: WechatMessage) -> Optional[E]:
        """"""
        if msg.message != "收到红包，请在手机上查看":
            return None
        return