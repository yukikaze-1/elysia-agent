"""Debug recording routes: prompt + response recorder inspection."""

import logging
import os

from aiohttp import web
from paths import ProjectPath

logger = logging.getLogger(__name__)


def register_recording_routes(app: web.Application) -> None:
    """注册 Prompts + Responses 路由。"""
    app.router.add_get("/debug/api/prompts", prompts_list_handler)
    app.router.add_get(r"/debug/api/prompts/{index:\d+}", prompt_detail_handler)
    app.router.add_get("/debug/api/responses", responses_list_handler)
    app.router.add_get(r"/debug/api/responses/{index:\d+}", response_detail_handler)


async def prompts_list_handler(request: web.Request) -> web.Response:
    """返回最近 N 次 LLM 调用的 prompt 摘要列表。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"records": [...], "total": ..., "max": ...}。
    """
    agent = request.app["agent"]
    recorder = getattr(agent, "prompt_recorder", None)
    if recorder is None:
        return web.json_response({"records": [], "total": 0, "max": 0})
    return web.json_response(recorder.list_summaries())


async def prompt_detail_handler(request: web.Request) -> web.Response:
    """返回指定记录的完整 prompt 内容 + 模板源码。

    Args:
        request: aiohttp Request, URL path: /{index}。

    Returns:
        web.Response: {"index": ..., "timestamp": ..., "messages": [...], "tools": [...], ...}。
    """
    index = int(request.match_info["index"])
    agent = request.app["agent"]
    recorder = getattr(agent, "prompt_recorder", None)
    if recorder is None:
        raise web.HTTPNotFound(text="PromptRecorder 未初始化")
    record = recorder.get_full(index)
    if record is None:
        raise web.HTTPNotFound(text=f"记录 #{index} 不存在")

    # 读取模板源码（从 Module/system_prompt/templates/）
    template_source = None
    templates_dir = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())
    try:
        sources = {}
        for fname in sorted(os.listdir(templates_dir)):
            if fname.endswith(".j2") and not fname.startswith("_"):
                fpath = os.path.join(templates_dir, fname)
                with open(fpath) as f:
                    sources[fname] = f.read()
        if sources:
            template_source = {"path": templates_dir, "sections": sources}
    except Exception:
        pass

    return web.json_response({
        "index": record.index,
        "timestamp": record.timestamp,
        "round_num": record.round_num,
        "model": record.model,
        "messages": record.messages,
        "tools": record.tools,
        "template_source": template_source,
    })


async def responses_list_handler(request: web.Request) -> web.Response:
    """返回最近 N 次 LLM 调用的原始回复摘要列表。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"records": [...], "total": ..., "max": ...}。
    """
    agent = request.app["agent"]
    recorder = getattr(agent, "response_recorder", None)
    if recorder is None:
        return web.json_response({"records": [], "total": 0, "max": 0})
    return web.json_response(recorder.list_summaries())


async def response_detail_handler(request: web.Request) -> web.Response:
    """返回指定记录的完整原始回复。

    Args:
        request: aiohttp Request, URL path: /{index}。

    Returns:
        web.Response: {"index": ..., "timestamp": ..., "raw_text": ..., "inner_voice": ...}。
    """
    index = int(request.match_info["index"])
    agent = request.app["agent"]
    recorder = getattr(agent, "response_recorder", None)
    if recorder is None:
        raise web.HTTPNotFound(text="ResponseRecorder 未初始化")
    record = recorder.get_full(index)
    if record is None:
        raise web.HTTPNotFound(text=f"记录 #{index} 不存在")
    return web.json_response({
        "index": record.index,
        "timestamp": record.timestamp,
        "round_num": record.round_num,
        "raw_text": record.raw_text,
        "inner_voice": record.inner_voice,
        "text_len": record.text_len,
        "inner_voice_len": record.inner_voice_len,
    })
