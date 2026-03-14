from __future__ import annotations

import asyncio
import importlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Optional

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.event_message_type import EventMessageType


PLUGIN_NAME = "astrbot_plugin_research_digest"
PLUGIN_VERSION = "0.4.0"
ARXIV_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_DESKTOP_DIR = "~/Desktop/Research_Paper_Summaries"
DEFAULT_COLLECTION = "research_paper_digest"
SCHOLAR_URL = "https://scholar.google.com/scholar"
ARXIV_URL = "https://export.arxiv.org/api/query"
GITHUB_URL = "https://api.github.com/search/repositories"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

SUMMARY_PROMPT = """
你是一个服务于 AstrBot 角色的“论文研究巡检插件”。
你的任务是把来自 arXiv、Google Scholar、GitHub 的原始证据整理成严谨、可信、全中文、便于深入理解技术核心的研究简报。

工作步骤：
1. 仔细阅读证据包，只能依据证据包中的信息做总结，不要编造论文内容。
2. 不要只写“做了什么”，还要写清楚“核心难点是什么、为什么以前的方法不行、这篇论文到底靠什么机制解决、具体如何实现”。
3. 对方法部分必须尽量落到可执行层：模块划分、输入输出、数据流、训练信号、优化目标、推理时序、关键约束、各组件如何配合。
4. 如果证据中没有明确给出数学公式，就明确写“未从证据中获取到公式”，不要幻觉补全；但要尽量解释目标函数、约束或隐含优化逻辑。
5. 尽量把术语、结构、表述都规范化，方便每天系统化浏览。
6. 优先提取具体信息：benchmark、训练设置、输入输出模态、数据集、动作空间、硬件平台、仿真环境、策略结构、优化目标。
7. 如果 GitHub 仓库相关，要说明它更像官方代码、复现项目、基准工具，还是只是弱相关项目。

返回要求：
1. 只能返回 JSON，不要加 markdown 代码块。
2. 所有字段内容都必须使用中文，`reading_priority` 除外，它只能是 `high`、`medium`、`low` 三选一。
3. JSON 结构必须严格符合下面这个 schema：
{
  "tldr": "一段中文总结",
  "problem_statement": "中文",
  "research_gap": "中文，说明以前方法的短板与本文切入点",
  "central_hypothesis": "中文，说明作者相信什么机制会奏效",
  "system_overview": "中文，2-4 句讲清整体方案",
  "innovation_points": ["中文要点", "..."],
  "method_breakdown": ["中文要点", "..."],
  "key_modules": [
    {
      "name": "模块名",
      "role": "负责什么",
      "input": "输入是什么",
      "output": "输出是什么",
      "details": "内部机制或与其他模块的配合方式"
    }
  ],
  "implementation_pipeline": ["按时间或数据流顺序拆解实现过程", "..."],
  "training_or_optimization": ["训练目标、损失、监督信号、优化过程", "..."],
  "inference_workflow": ["推理阶段如何一步步运行", "..."],
  "why_it_works": ["解释为什么这些设计能解决问题", "..."],
  "core_equations": [
    {
      "name": "公式或目标函数名称",
      "formula": "如果证据中有公式就填公式，没有就填空字符串",
      "meaning": "中文解释"
    }
  ],
  "experiments_and_results": ["中文要点", "..."],
  "limitations": ["中文要点", "..."],
  "technical_takeaways": ["读完后最值得记住的技术抓手", "..."],
  "evidence_quality": ["哪些内容证据充分，哪些只是根据摘要推断", "..."],
  "topic_relevance": "中文",
  "repo_assessment": ["中文要点", "..."],
  "follow_up_questions": ["中文要点", "..."],
  "reading_priority": "high|medium|low"
}
""".strip()


@dataclass
class RepoCandidate:
    name: str
    url: str
    description: str = ""
    stars: int = 0
    updated_at: str = ""


@dataclass
class PaperCandidate:
    title: str
    url: str
    abstract: str = ""
    pdf_url: str = ""
    authors: list[str] = field(default_factory=list)
    published: str = ""
    updated: str = ""
    source: str = ""
    scholar_snippet: str = ""
    scholar_meta: str = ""
    github_repos: list[RepoCandidate] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class PaperSummary:
    title: str
    paper_url: str
    pdf_url: str
    authors: list[str]
    published: str
    updated: str
    source: str
    tldr: str
    problem_statement: str
    research_gap: str
    central_hypothesis: str
    system_overview: str
    innovation_points: list[str]
    method_breakdown: list[str]
    key_modules: list[dict[str, str]]
    implementation_pipeline: list[str]
    training_or_optimization: list[str]
    inference_workflow: list[str]
    why_it_works: list[str]
    core_equations: list[dict[str, str]]
    experiments_and_results: list[str]
    limitations: list[str]
    technical_takeaways: list[str]
    evidence_quality: list[str]
    topic_relevance: str
    repo_assessment: list[str]
    follow_up_questions: list[str]
    reading_priority: str
    abstract: str
    scholar_snippet: str
    scholar_meta: str
    github_repos: list[RepoCandidate]


@register(
    PLUGIN_NAME,
    "Codex",
    "Generic research paper scout with daily markdown briefs and knowledge-base sync",
    PLUGIN_VERSION,
    "https://github.com/young8290/astrbot_plugin_research_digest",
)
class ResearchDigestPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.state_file = self.data_dir / "state.json"
        self.run_lock = asyncio.Lock()
        self.monitor_task: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        self.worker_task: asyncio.Task | None = None
        self.http: httpx.AsyncClient | None = None
        self.run_queue: asyncio.Queue[str] = asyncio.Queue()
        self.pending_reasons: set[str] = set()
        self.active_reason: str | None = None
        self.started_at = time.time()
        self.state: dict[str, Any] = {
            "last_user_activity": 0.0,
            "last_run_date": "",
            "last_run_at": "",
            "last_run_reason": "",
            "last_run_status": "never",
            "last_error": "",
            "last_generated_files": [],
            "last_candidate_count": 0,
            "last_paper_count": 0,
            "last_repo_radar_count": 0,
            "last_notification_status": "",
            "last_scheduler_tick": "",
            "last_enqueued_reason": "",
            "run_history": [],
        }
        self.admin_ids = [str(x) for x in self.context.get_config().get("admins_id", [])]

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()
        if not self.state.get("last_user_activity"):
            self.state["last_user_activity"] = time.time()
            self._save_state()
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                float(self._cfg("network.request_timeout_seconds", 20.0)),
                connect=float(self._cfg("network.connect_timeout_seconds", 15.0)),
            ),
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        self.worker_task = asyncio.create_task(self._worker_loop())
        if self._cfg("runtime.enable_startup_run", True):
            self.startup_task = asyncio.create_task(self._startup_auto_run())
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "[%s] plugin initialized | watched=%s | idle=%sh | poll=%sm | startup=%s | fixed_time=%s",
            PLUGIN_NAME,
            ",".join(self._get_watched_user_ids()) or "(none)",
            self._cfg("runtime.inactivity_hours", 12),
            self._cfg("runtime.poll_interval_minutes", 30),
            self._cfg("runtime.enable_startup_run", True),
            self._cfg("runtime.fixed_daily_time", "") or "(none)",
        )

    async def terminate(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        if self.startup_task:
            self.startup_task.cancel()
            try:
                await self.startup_task
            except asyncio.CancelledError:
                pass
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        if self.http:
            await self.http.aclose()
            self.http = None
        self._save_state()

    @filter.event_message_type(EventMessageType.ALL, priority=950)
    async def track_user_activity(self, event: AstrMessageEvent):
        watched_ids = self._get_watched_user_ids()
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id and sender_id in watched_ids:
            self.state["last_user_activity"] = time.time()
            self._save_state()

    @filter.command_group("digest", alias={"embodied", "paper", "research", "论文"})
    def digest_group(self):
        """Research digest commands."""

    @digest_group.command("run")
    async def digest_run(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以手动触发论文巡检任务。")
            return
        ok, message = await self._run_pipeline("manual")
        yield event.plain_result(message if ok else f"执行失败：{message}")

    @digest_group.command("status")
    async def digest_status(self, event: AstrMessageEvent):
        watched = ", ".join(self._get_watched_user_ids()) or "(未设置)"
        inactivity = round(
            max(time.time() - float(self.state.get("last_user_activity", 0.0)), 0.0) / 3600,
            2,
        )
        enabled_sources = [
            name
            for name, flag in [
                ("arXiv", self._cfg("research.enable_arxiv", True)),
                ("Google Scholar", self._cfg("research.enable_google_scholar", True)),
                ("GitHub", self._cfg("research.enable_github", True)),
            ]
            if flag
        ]
        lines = [
            f"自动巡检：{self._cfg('runtime.enabled', True)}",
            f"知识库集合：{self._cfg('outputs.collection_name', DEFAULT_COLLECTION)}",
            f"当前主题：{self._topic_label()}",
            f"监控 QQ：{watched}",
            f"启用来源：{', '.join(enabled_sources) or '(未启用)'}",
            f"每日论文数量：{self._cfg('research.max_papers_per_run', 4)}",
            f"启动后自动首跑：{self._cfg('runtime.enable_startup_run', True)}",
            f"固定执行时间：{self._cfg('runtime.fixed_daily_time', '') or '(未设置)'}",
            f"上次执行日期：{self.state.get('last_run_date', '') or '(从未执行)'}",
            f"上次执行原因：{self._format_reason(self.state.get('last_run_reason', '')) or '(无)'}",
            f"上次执行状态：{self.state.get('last_run_status', 'unknown')}",
            f"上次候选数：{self.state.get('last_candidate_count', 0)}",
            f"上次生成论文数：{self.state.get('last_paper_count', 0)}",
            f"上次 GitHub 雷达数：{self.state.get('last_repo_radar_count', 0)}",
            f"上次通知状态：{self.state.get('last_notification_status', '') or '(无)'}",
            f"上次错误：{self.state.get('last_error', '') or '(无)'}",
            f"当前空闲小时：{inactivity}",
        ]
        yield event.plain_result("\n".join(lines))

    @digest_group.command("prompt")
    async def digest_prompt(self, event: AstrMessageEvent):
        yield event.plain_result(self._active_summary_prompt())

    @digest_group.command("doctor")
    async def digest_doctor(self, event: AstrMessageEvent):
        watched = ", ".join(self._get_watched_user_ids()) or "(未设置)"
        notify_targets = ", ".join(self._notify_targets()) or "(未设置)"
        kb_meta = self.context.get_registered_star("astrbot_plugin_knowledge_base")
        provider = self._get_summary_provider()
        provider_label = "(默认/未找到)"
        if provider:
            try:
                provider_meta = provider.meta()
                provider_label = getattr(provider_meta, "id", None) or str(provider_meta)
            except Exception:
                provider_label = str(provider)
        lines = [
            f"插件版本：{PLUGIN_VERSION}",
            f"主题：{self._topic_label()}",
            f"监控 QQ：{watched}",
            f"通知目标：{notify_targets}",
            f"自动巡检：{self._cfg('runtime.enabled', True)}",
            f"启动首跑：{self._cfg('runtime.enable_startup_run', True)}",
            f"启动延迟（分钟）：{self._cfg('runtime.startup_run_delay_minutes', 3)}",
            f"固定执行时间：{self._cfg('runtime.fixed_daily_time', '') or '(未设置)'}",
            f"空闲触发小时：{self._cfg('runtime.inactivity_hours', 12)}",
            f"轮询间隔（分钟）：{self._cfg('runtime.poll_interval_minutes', 30)}",
            f"失败补跑（分钟）：{self._cfg('runtime.retry_on_failure_minutes', 90)}",
            f"空结果补跑（分钟）：{self._cfg('runtime.retry_on_empty_minutes', 180)}",
            f"输出目录：{self._resolve_output_dir()}",
            f"知识库镜像目录：{self._kb_import_dir()}",
            f"知识库同步：{self._cfg('outputs.sync_to_knowledge_base', True)}",
            f"知识库插件可用：{bool(kb_meta and kb_meta.star_cls)}",
            f"摘要 Provider：{provider_label}",
            f"上次调度心跳：{self.state.get('last_scheduler_tick', '') or '(无)'}",
            f"上次入队原因：{self._format_reason(self.state.get('last_enqueued_reason', '')) or '(无)'}",
            f"上次执行时间：{self.state.get('last_run_at', '') or '(从未执行)'}",
            f"上次执行状态：{self.state.get('last_run_status', 'unknown')}",
            f"上次错误：{self.state.get('last_error', '') or '(无)'}",
        ]
        yield event.plain_result("\n".join(lines))

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(60, int(self._cfg("runtime.poll_interval_minutes", 30)) * 60))
                self.state["last_scheduler_tick"] = datetime.now().isoformat(timespec="seconds")
                self._save_state()
                auto_reason = self._get_auto_run_reason()
                if auto_reason:
                    logger.info("[%s] auto run triggered by %s", PLUGIN_NAME, auto_reason)
                    await self._enqueue_run(auto_reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[%s] monitor loop failed: %s", PLUGIN_NAME, exc, exc_info=True)

    async def _startup_auto_run(self) -> None:
        try:
            delay_seconds = max(0, int(self._cfg("runtime.startup_run_delay_minutes", 3)) * 60)
            logger.info("[%s] startup auto run armed, delay=%ss", PLUGIN_NAME, delay_seconds)
            await asyncio.sleep(delay_seconds)
            if self._cfg("runtime.run_only_once_per_day", True) and self.state.get("last_run_date") == self._today_str():
                logger.info("[%s] startup auto run skipped because today's run already exists", PLUGIN_NAME)
                return
            logger.info("[%s] startup auto run executing", PLUGIN_NAME)
            await self._enqueue_run("startup")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[%s] startup auto run failed: %s", PLUGIN_NAME, exc, exc_info=True)

    async def _worker_loop(self) -> None:
        while True:
            reason = await self.run_queue.get()
            self.active_reason = reason
            try:
                logger.info("[%s] worker started for reason=%s", PLUGIN_NAME, reason)
                await self._run_pipeline(reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[%s] worker failed for reason=%s: %s", PLUGIN_NAME, reason, exc, exc_info=True)
            finally:
                self.pending_reasons.discard(reason)
                self.active_reason = None
                self.run_queue.task_done()

    async def _enqueue_run(self, reason: str, *, force: bool = False) -> bool:
        reason = str(reason).strip()
        if not reason:
            return False
        if self.active_reason == reason or reason in self.pending_reasons:
            logger.info("[%s] skip enqueue duplicated reason=%s", PLUGIN_NAME, reason)
            return False
        if (
            not force
            and reason != "manual"
            and self._cfg("runtime.run_only_once_per_day", True)
            and self.state.get("last_run_date") == self._today_str()
            and self.state.get("last_run_status") == "success"
        ):
            logger.info("[%s] skip enqueue %s because today's successful run already exists", PLUGIN_NAME, reason)
            return False
        self.pending_reasons.add(reason)
        self.state["last_enqueued_reason"] = reason
        self._save_state()
        await self.run_queue.put(reason)
        logger.info("[%s] enqueued run reason=%s pending=%s", PLUGIN_NAME, reason, sorted(self.pending_reasons))
        return True

    def _get_auto_run_reason(self) -> str | None:
        if not self._cfg("runtime.enabled", True):
            return None
        today = self._today_str()
        retry_reason = self._get_retry_reason(today)
        if retry_reason:
            return retry_reason
        if self._cfg("runtime.run_only_once_per_day", True) and self.state.get("last_run_date") == today:
            return None

        if self._cfg("runtime.enable_startup_run", True):
            startup_delay_seconds = int(self._cfg("runtime.startup_run_delay_minutes", 3)) * 60
            if time.time() - self.started_at >= startup_delay_seconds:
                if not self.state.get("last_run_date"):
                    return "startup"

        fixed_daily_time = str(self._cfg("runtime.fixed_daily_time", "")).strip()
        if fixed_daily_time:
            schedule_dt = self._scheduled_datetime_today(fixed_daily_time)
            last_run_at = self._state_datetime("last_run_at")
            if schedule_dt and datetime.now() >= schedule_dt and (
                not last_run_at or last_run_at.date() != datetime.now().date() or last_run_at < schedule_dt
            ):
                return "daily_time"

        idle_seconds = time.time() - float(self.state.get("last_user_activity", 0.0))
        required_idle = int(self._cfg("runtime.inactivity_hours", 12)) * 3600
        if idle_seconds >= required_idle:
            return "idle"
        return None

    def _get_retry_reason(self, today: str) -> str | None:
        if self.state.get("last_run_date") != today:
            return None
        last_run_at = self._state_datetime("last_run_at")
        if not last_run_at:
            return None
        minutes_since = (datetime.now() - last_run_at).total_seconds() / 60
        status = str(self.state.get("last_run_status", "")).strip()
        if status == "failed":
            retry_after = int(self._cfg("runtime.retry_on_failure_minutes", 90))
            if retry_after > 0 and minutes_since >= retry_after:
                return "retry_failure"
        if status == "empty":
            retry_after = int(self._cfg("runtime.retry_on_empty_minutes", 180))
            if retry_after > 0 and minutes_since >= retry_after:
                return "retry_empty"
        return None

    async def _run_pipeline(self, reason: str) -> tuple[bool, str]:
        if self.run_lock.locked():
            return False, "已有论文巡检任务正在运行。"

        async with self.run_lock:
            try:
                logger.info("[%s] pipeline started | reason=%s", PLUGIN_NAME, reason)
                outputs_dir = self._resolve_output_dir()
                day_dir = outputs_dir / self._today_str()
                kb_import_root = self._kb_import_dir() if self._cfg("outputs.write_kb_import_markdown", True) else None
                kb_import_dir = kb_import_root / self._today_str() if kb_import_root else None
                day_dir.mkdir(parents=True, exist_ok=True)
                if kb_import_dir:
                    kb_import_dir.mkdir(parents=True, exist_ok=True)

                candidates = await self._collect_candidates()
                repo_radar: list[RepoCandidate] = []
                if self._cfg("research.enable_github", True):
                    repo_radar = await self._fetch_github_repos(
                        " OR ".join(self._focus_queries()),
                        int(self._cfg("research.github_results_per_query", 5)),
                    )
                self.state["last_candidate_count"] = len(candidates)
                self.state["last_repo_radar_count"] = len(repo_radar)
                logger.info(
                    "[%s] pipeline collected candidates=%s repo_radar=%s",
                    PLUGIN_NAME,
                    len(candidates),
                    len(repo_radar),
                )
                if not candidates:
                    index_md = self._render_daily_index([], repo_radar, reason)
                    index_path = day_dir / "_index.md"
                    index_path.write_text(index_md, encoding="utf-8")
                    generated_files = [str(index_path)]
                    if kb_import_dir:
                        kb_index_path = kb_import_dir / "_index.md"
                        kb_index_path.write_text(index_md, encoding="utf-8")
                        generated_files.append(str(kb_index_path))
                    if self._cfg("outputs.write_manifest_json", True):
                        manifest_path = day_dir / "manifest.json"
                        manifest_path.write_text(
                            json.dumps(
                                self._build_manifest(reason, [], [], repo_radar, generated_files),
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        generated_files.append(str(manifest_path))
                    kb_message = "未写入知识库。"
                    if self._cfg("outputs.sync_to_knowledge_base", True):
                        kb_message = await self._sync_to_knowledge_base([], index_md, day_dir)
                    await self._notify_result(
                        title=f"{self._topic_label()} 论文巡检",
                        body=self._build_notification_text(reason, [], repo_radar, generated_files, kb_message),
                        notify_key="empty",
                    )
                    self.state["last_paper_count"] = 0
                    self._record_run("empty", reason, "没有找到符合条件的研究候选。", generated_files)
                    logger.info("[%s] pipeline finished empty", PLUGIN_NAME)
                    return True, "本次没有找到符合条件的研究候选，已生成空日报。"

                selected = candidates[: max(1, int(self._cfg("research.max_papers_per_run", 4)))]

                summaries: list[PaperSummary] = []
                generated_files: list[str] = []
                for index, paper in enumerate(selected, start=1):
                    scholar_match = None
                    if self._cfg("research.enable_google_scholar", True):
                        scholar_match = await self._fetch_scholar_for_title(paper.title)
                    if scholar_match:
                        if scholar_match.scholar_snippet:
                            paper.scholar_snippet = scholar_match.scholar_snippet
                        if scholar_match.scholar_meta:
                            paper.scholar_meta = scholar_match.scholar_meta
                    if self._cfg("research.enable_github", True):
                        paper.github_repos = await self._fetch_github_repos(
                            self._github_query_for_paper(paper.title),
                            int(self._cfg("research.github_repos_per_paper", 3)),
                        )
                    else:
                        paper.github_repos = []
                    summary = await self._summarize_paper(paper, repo_radar)
                    summaries.append(summary)
                    paper_md = self._render_paper_markdown(summary)
                    paper_path = day_dir / f"{index:02d}-{self._slugify(summary.title)}.md"
                    paper_path.write_text(paper_md, encoding="utf-8")
                    generated_files.append(str(paper_path))
                    if kb_import_dir:
                        kb_path = kb_import_dir / paper_path.name
                        kb_path.write_text(paper_md, encoding="utf-8")
                        generated_files.append(str(kb_path))

                index_md = self._render_daily_index(summaries, repo_radar, reason)
                index_path = day_dir / "_index.md"
                index_path.write_text(index_md, encoding="utf-8")
                generated_files.append(str(index_path))
                if kb_import_dir:
                    kb_index_path = kb_import_dir / "_index.md"
                    kb_index_path.write_text(index_md, encoding="utf-8")
                    generated_files.append(str(kb_index_path))
                if self._cfg("outputs.write_manifest_json", True):
                    manifest_path = day_dir / "manifest.json"
                    manifest_path.write_text(
                        json.dumps(
                            self._build_manifest(reason, candidates, summaries, repo_radar, generated_files),
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    generated_files.append(str(manifest_path))

                if self._cfg("outputs.sync_to_knowledge_base", True):
                    kb_message = await self._sync_to_knowledge_base(summaries, index_md, day_dir)
                else:
                    kb_message = "已关闭知识库写入，只生成了 Markdown 摘要。"
                self.state["last_paper_count"] = len(summaries)
                await self._notify_result(
                    title=f"{self._topic_label()} 论文巡检已完成",
                    body=self._build_notification_text(reason, summaries, repo_radar, generated_files, kb_message),
                    notify_key="success",
                )
                self._record_run("success", reason, kb_message, generated_files)
                logger.info("[%s] pipeline finished successfully papers=%s", PLUGIN_NAME, len(summaries))
                return True, f"论文巡检完成。{kb_message}"
            except Exception as exc:
                logger.error("[%s] pipeline failed: %s", PLUGIN_NAME, exc, exc_info=True)
                self.state["last_candidate_count"] = 0
                self.state["last_paper_count"] = 0
                self.state["last_repo_radar_count"] = 0
                await self._notify_result(
                    title=f"{self._topic_label()} 论文巡检失败",
                    body=f"执行原因：{self._format_reason(reason)}\n错误：{exc}",
                    notify_key="failure",
                )
                self._record_run("failed", reason, f"{exc}", [])
                return False, f"{exc}"

    async def _collect_candidates(self) -> list[PaperCandidate]:
        arxiv_candidates: list[PaperCandidate] = []
        scholar_candidates: list[PaperCandidate] = []
        per_query_arxiv = int(self._cfg("research.arxiv_results_per_query", 6))
        per_query_scholar = int(self._cfg("research.scholar_results_per_query", 4))

        for query in self._focus_queries():
            arxiv_count_before = len(arxiv_candidates)
            scholar_count_before = len(scholar_candidates)
            if self._cfg("research.enable_arxiv", True):
                arxiv_candidates.extend(await self._fetch_arxiv(query, per_query_arxiv))
            if self._cfg("research.enable_google_scholar", True):
                await asyncio.sleep(float(self._cfg("network.scholar_request_interval_seconds", 1.0)))
                scholar_candidates.extend(await self._fetch_google_scholar(query, per_query_scholar))
            logger.info(
                "[%s] query=%s fetched arxiv=%s scholar=%s",
                PLUGIN_NAME,
                query,
                len(arxiv_candidates) - arxiv_count_before,
                len(scholar_candidates) - scholar_count_before,
            )

        merged: dict[str, PaperCandidate] = {}
        for candidate in arxiv_candidates + scholar_candidates:
            key = self._normalize_title(candidate.title)
            if not key:
                continue
            if key not in merged:
                merged[key] = candidate
                continue
            current = merged[key]
            if not current.abstract and candidate.abstract:
                current.abstract = candidate.abstract
            if not current.pdf_url and candidate.pdf_url:
                current.pdf_url = candidate.pdf_url
            if not current.url and candidate.url:
                current.url = candidate.url
            if not current.authors and candidate.authors:
                current.authors = candidate.authors
            if not current.scholar_snippet and candidate.scholar_snippet:
                current.scholar_snippet = candidate.scholar_snippet
            if not current.scholar_meta and candidate.scholar_meta:
                current.scholar_meta = candidate.scholar_meta
            current.keywords = list(sorted(set(current.keywords + candidate.keywords)))
            current.source = ",".join(
                sorted({value for value in [current.source, candidate.source] if value})
            )

        recent_cutoff = datetime.now() - timedelta(days=int(self._cfg("research.recent_days", 7)))
        scored = []
        for candidate in merged.values():
            if candidate.updated:
                try:
                    updated_dt = datetime.fromisoformat(candidate.updated.replace("Z", "+00:00"))
                    if updated_dt.replace(tzinfo=None) < recent_cutoff:
                        continue
                except ValueError:
                    pass
            score = self._score_candidate(candidate)
            scored.append((score, candidate))

        scored.sort(key=lambda item: item[0], reverse=True)
        logger.info("[%s] merged candidates=%s", PLUGIN_NAME, len(scored))
        return [candidate for _, candidate in scored]

    async def _fetch_arxiv(self, query: str, limit: int) -> list[PaperCandidate]:
        client = self._require_http()
        params = {
            "search_query": f'all:"{query}"',
            "start": 0,
            "max_results": limit,
            "sortBy": "lastUpdatedDate",
            "sortOrder": "descending",
        }
        try:
            response = await client.get(ARXIV_URL, params=params)
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except Exception as exc:
            logger.warning("[%s] arXiv fetch failed for %s: %s", PLUGIN_NAME, query, exc)
            return []

        entries: list[PaperCandidate] = []
        for entry in root.findall("atom:entry", ARXIV_ATOM_NS):
            title = self._clean_text(entry.findtext("atom:title", default="", namespaces=ARXIV_ATOM_NS))
            summary = self._clean_text(entry.findtext("atom:summary", default="", namespaces=ARXIV_ATOM_NS))
            published = self._clean_text(entry.findtext("atom:published", default="", namespaces=ARXIV_ATOM_NS))
            updated = self._clean_text(entry.findtext("atom:updated", default="", namespaces=ARXIV_ATOM_NS))
            links = entry.findall("atom:link", ARXIV_ATOM_NS)
            paper_url = ""
            pdf_url = ""
            for link in links:
                href = link.attrib.get("href", "")
                rel = link.attrib.get("rel", "")
                title_attr = link.attrib.get("title", "")
                if rel == "alternate" and href:
                    paper_url = href
                if title_attr == "pdf" and href:
                    pdf_url = href
            authors = [
                self._clean_text(author.findtext("atom:name", default="", namespaces=ARXIV_ATOM_NS))
                for author in entry.findall("atom:author", ARXIV_ATOM_NS)
            ]
            entries.append(
                PaperCandidate(
                    title=title,
                    url=paper_url,
                    abstract=summary,
                    pdf_url=pdf_url,
                    authors=[author for author in authors if author],
                    published=published,
                    updated=updated,
                    source="arxiv",
                    keywords=[query],
                )
            )
        return entries

    async def _fetch_google_scholar(self, query: str, limit: int) -> list[PaperCandidate]:
        client = self._require_http()
        params = {"hl": "en", "q": query}
        try:
            response = await client.get(SCHOLAR_URL, params=params)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("[%s] Google Scholar fetch failed for %s: %s", PLUGIN_NAME, query, exc)
            return []

        html = response.text
        pattern = re.compile(
            r'<div class="gs_ri".*?<h3 class="gs_rt">(.*?)</h3>.*?<div class="gs_a">(.*?)</div>(?:.*?<div class="gs_rs"[^>]*>(.*?)</div>)?',
            re.S,
        )
        results: list[PaperCandidate] = []
        for match in pattern.finditer(html):
            title_html, meta_html, snippet_html = match.groups()
            title = self._strip_html(title_html)
            url_match = re.search(r'href="([^"]+)"', title_html or "")
            url = unescape(url_match.group(1)) if url_match else ""
            meta = self._strip_html(meta_html or "")
            snippet = self._strip_html(snippet_html or "")
            results.append(
                PaperCandidate(
                    title=title,
                    url=url,
                    abstract=snippet,
                    source="google_scholar",
                    scholar_snippet=snippet,
                    scholar_meta=meta,
                    keywords=[query],
                )
            )
            if len(results) >= limit:
                break
        return results

    async def _fetch_scholar_for_title(self, title: str) -> Optional[PaperCandidate]:
        matches = await self._fetch_google_scholar(f'"{title}"', 1)
        return matches[0] if matches else None

    async def _fetch_github_repos(self, query: str, limit: int) -> list[RepoCandidate]:
        client = self._require_http()
        headers = {}
        token = str(self._cfg("providers.github_token", "")).strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params = {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": max(1, min(limit, 10)),
        }
        try:
            response = await client.get(GITHUB_URL, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("[%s] GitHub fetch failed for %s: %s", PLUGIN_NAME, query, exc)
            return []

        repos: list[RepoCandidate] = []
        for item in payload.get("items", [])[:limit]:
            repos.append(
                RepoCandidate(
                    name=item.get("full_name", ""),
                    url=item.get("html_url", ""),
                    description=item.get("description", "") or "",
                    stars=int(item.get("stargazers_count", 0)),
                    updated_at=item.get("updated_at", "") or "",
                )
            )
        return repos

    async def _summarize_paper(
        self, candidate: PaperCandidate, repo_radar: list[RepoCandidate]
    ) -> PaperSummary:
        provider = self._get_summary_provider()
        prompt = self._build_prompt(candidate, repo_radar)

        parsed: dict[str, Any] | None = None
        if provider:
            try:
                response = await provider.text_chat(
                    prompt=prompt,
                    session_id=f"{PLUGIN_NAME}_{self._slugify(candidate.title)}",
                    contexts=[],
                    model=str(self._cfg("providers.summary_model", "")).strip() or None,
                )
                parsed = self._extract_json((response.completion_text or "").strip())
            except TypeError:
                try:
                    response = await provider.text_chat(
                        prompt=prompt,
                        session_id=f"{PLUGIN_NAME}_{self._slugify(candidate.title)}",
                        contexts=[],
                    )
                    parsed = self._extract_json((response.completion_text or "").strip())
                except Exception as exc:
                    logger.warning("[%s] summary fallback call failed: %s", PLUGIN_NAME, exc)
            except Exception as exc:
                logger.warning("[%s] summary generation failed for %s: %s", PLUGIN_NAME, candidate.title, exc)

        if not parsed:
            parsed = self._fallback_summary_payload(candidate)

        return PaperSummary(
            title=candidate.title,
            paper_url=candidate.url,
            pdf_url=candidate.pdf_url,
            authors=candidate.authors,
            published=candidate.published,
            updated=candidate.updated,
            source=candidate.source,
            tldr=self._safe_text(parsed.get("tldr")),
            problem_statement=self._safe_text(parsed.get("problem_statement")),
            research_gap=self._safe_text(parsed.get("research_gap")),
            central_hypothesis=self._safe_text(parsed.get("central_hypothesis")),
            system_overview=self._safe_text(parsed.get("system_overview")),
            innovation_points=self._coerce_list(parsed.get("innovation_points")),
            method_breakdown=self._coerce_list(parsed.get("method_breakdown")),
            key_modules=self._coerce_modules(parsed.get("key_modules")),
            implementation_pipeline=self._coerce_list(parsed.get("implementation_pipeline")),
            training_or_optimization=self._coerce_list(parsed.get("training_or_optimization")),
            inference_workflow=self._coerce_list(parsed.get("inference_workflow")),
            why_it_works=self._coerce_list(parsed.get("why_it_works")),
            core_equations=self._coerce_equations(parsed.get("core_equations")),
            experiments_and_results=self._coerce_list(parsed.get("experiments_and_results")),
            limitations=self._coerce_list(parsed.get("limitations")),
            technical_takeaways=self._coerce_list(parsed.get("technical_takeaways")),
            evidence_quality=self._coerce_list(parsed.get("evidence_quality")),
            topic_relevance=self._safe_text(
                parsed.get("topic_relevance") or parsed.get("embodied_ai_relevance")
            ),
            repo_assessment=self._coerce_list(parsed.get("repo_assessment")),
            follow_up_questions=self._coerce_list(parsed.get("follow_up_questions")),
            reading_priority=self._safe_text(parsed.get("reading_priority")) or "medium",
            abstract=candidate.abstract,
            scholar_snippet=candidate.scholar_snippet,
            scholar_meta=candidate.scholar_meta,
            github_repos=candidate.github_repos,
        )

    async def _sync_to_knowledge_base(
        self, summaries: list[PaperSummary], index_md: str, day_dir: Path
    ) -> str:
        try:
            kb_meta = self.context.get_registered_star("astrbot_plugin_knowledge_base")
            if not kb_meta or not kb_meta.star_cls:
                return "知识库插件不可用，但 Markdown 摘要已经生成。"

            kb_plugin = kb_meta.star_cls
            ensure_fn = getattr(kb_plugin, "_ensure_initialized", None)
            if ensure_fn and not await ensure_fn():
                return "知识库插件尚未初始化，已跳过知识库写入。"

            document_cls = None
            for module_name in (
                "astrbot_plugin_knowledge_base.vector_store.base",
                "data.plugins.astrbot_plugin_knowledge_base.vector_store.base",
            ):
                try:
                    module = importlib.import_module(module_name)
                    document_cls = getattr(module, "Document", None)
                    if document_cls:
                        break
                except Exception:
                    continue
            if not document_cls:
                return "知识库 Document 类型不可用，已跳过知识库写入。"

            collection_name = self._cfg("outputs.collection_name", DEFAULT_COLLECTION)
            vector_db = getattr(kb_plugin, "vector_db", None)
            text_splitter = getattr(kb_plugin, "text_splitter", None)
            if not vector_db or not text_splitter:
                return "知识库插件缺少 vector_db 或 text_splitter，已跳过知识库写入。"

            if not await vector_db.collection_exists(collection_name):
                await vector_db.create_collection(collection_name)

            all_documents = []
            for summary in summaries:
                markdown = self._render_paper_markdown(summary)
                for idx, chunk in enumerate(text_splitter.split_text(markdown)):
                    all_documents.append(
                        document_cls(
                            text_content=chunk,
                            metadata={
                                "source": f"{summary.title}.md",
                                "paper_url": summary.paper_url,
                                "pdf_url": summary.pdf_url,
                                "day_dir": str(day_dir),
                                "chunk_id": idx,
                                "plugin": PLUGIN_NAME,
                            },
                        )
                    )

            for idx, chunk in enumerate(text_splitter.split_text(index_md)):
                all_documents.append(
                    document_cls(
                        text_content=chunk,
                        metadata={
                            "source": "_index.md",
                            "day_dir": str(day_dir),
                            "chunk_id": idx,
                            "plugin": PLUGIN_NAME,
                        },
                    )
                )

            if not all_documents:
                return "没有可写入知识库的文档分片。"
            doc_ids = await vector_db.add_documents(collection_name, all_documents)
            success_count = len(doc_ids)
            if success_count <= 0:
                logger.warning(
                    "[%s] knowledge base sync produced zero successful chunks | collection=%s total=%s",
                    PLUGIN_NAME,
                    collection_name,
                    len(all_documents),
                )
                return (
                    f"知识库向量写入未成功（0/{len(all_documents)} 个分片），"
                    "但 Markdown 摘要和知识库镜像文件已经生成。"
                )
            if success_count < len(all_documents):
                logger.warning(
                    "[%s] knowledge base sync partially succeeded | collection=%s success=%s total=%s",
                    PLUGIN_NAME,
                    collection_name,
                    success_count,
                    len(all_documents),
                )
                return (
                    f"知识库集合 `{collection_name}` 部分写入成功，"
                    f"成功 {success_count}/{len(all_documents)} 个分片。"
                )
            logger.info(
                "[%s] knowledge base synced collection=%s chunks=%s",
                PLUGIN_NAME,
                collection_name,
                success_count,
            )
            return f"已写入知识库集合 `{collection_name}`，共 {success_count} 个分片。"
        except Exception as exc:
            logger.warning("[%s] knowledge base sync failed: %s", PLUGIN_NAME, exc, exc_info=True)
            return f"知识库写入失败：{exc}，但 Markdown 摘要已经生成。"

    def _build_prompt(
        self, candidate: PaperCandidate, repo_radar: list[RepoCandidate]
    ) -> str:
        evidence = {
            "topic_label": self._topic_label(),
            "title": candidate.title,
            "paper_url": candidate.url,
            "pdf_url": candidate.pdf_url,
            "authors": candidate.authors,
            "published": candidate.published,
            "updated": candidate.updated,
            "source": candidate.source,
            "keywords": candidate.keywords,
            "abstract": candidate.abstract,
            "google_scholar_snippet": candidate.scholar_snippet,
            "google_scholar_meta": candidate.scholar_meta,
            "paper_related_github": [asdict(repo) for repo in candidate.github_repos],
            "global_github_radar": [asdict(repo) for repo in repo_radar],
        }
        prompt = (
            f"{self._active_summary_prompt()}\n\n"
            f"当前关注主题：{self._topic_label()}\n\n"
            f"证据包：\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
        )
        return prompt

    def _active_summary_prompt(self) -> str:
        prompt_override = str(self._cfg("prompts.summary_prompt_override", "")).strip()
        prompt_prefix = str(self._cfg("prompts.summary_prompt_prefix", "")).strip()
        prompt_suffix = str(self._cfg("prompts.summary_prompt_suffix", "")).strip()
        daily_focus_note = str(self._cfg("prompts.daily_focus_note", "")).strip()

        parts = []
        if prompt_prefix:
            parts.append(prompt_prefix)
        parts.append(prompt_override or SUMMARY_PROMPT)
        if daily_focus_note:
            parts.append(f"今日关注重点：\n{daily_focus_note}")
        if prompt_suffix:
            parts.append(f"额外要求：\n{prompt_suffix}")
        return "\n\n".join(part for part in parts if part).strip()

    def _fallback_summary_payload(self, candidate: PaperCandidate) -> dict[str, Any]:
        return {
            "tldr": candidate.abstract or "当前没有拿到足够摘要信息，建议后续人工补读原文。",
            "problem_statement": candidate.abstract or "当前证据不足，需要人工进一步确认论文问题定义。",
            "research_gap": "当前证据不足，只能确认论文试图解决某个已有方法尚未覆盖或表现不足的问题。",
            "central_hypothesis": "作者大概率认为，引入新的结构设计、训练方式或任务分解能够改善现有问题，但现有证据不足以精确还原。",
            "system_overview": "当前没有拿到足够证据去重建完整系统流程，建议结合摘要、论文 PDF 和代码仓库继续核对。",
            "innovation_points": [
                "本次总结回退到了源站元数据，因为模型总结结果暂时不可用。",
                "可以先结合摘要与仓库链接做人工补充。",
            ],
            "method_breakdown": [
                "建议优先查看论文摘要和相关仓库，以补全训练流程和方法细节。",
            ],
            "key_modules": [
                {
                    "name": "待补充",
                    "role": "当前证据不足，无法确认关键模块划分。",
                    "input": "待补充",
                    "output": "待补充",
                    "details": "建议结合论文图示、算法框图和代码结构继续补全。",
                }
            ],
            "implementation_pipeline": [
                "当前只拿到元数据，无法可靠拆出完整实现流水线。",
            ],
            "training_or_optimization": [
                "建议优先查看论文摘要和方法章节，以确认训练目标、损失函数和监督信号。",
            ],
            "inference_workflow": [
                "建议结合方法图和实验设置，补充推理阶段的模块调用顺序。",
            ],
            "why_it_works": [
                "当前证据不足，只能推测作者通过新的模块设计、任务分解或训练策略提升性能。",
            ],
            "core_equations": [
                {
                    "name": "未获取到公式",
                    "formula": "",
                    "meaning": "当前抓取到的证据中没有明确展示可引用的数学公式。",
                }
            ],
            "experiments_and_results": ["建议继续查阅摘要原文、论文页面或仓库说明，以确认具体实验设置和 benchmark。"],
            "limitations": ["当前为回退摘要，信息完整度有限。"],
            "technical_takeaways": ["在缺少全文证据时，当前最稳妥的做法是把论文先归档，再人工补读关键章节。"],
            "evidence_quality": ["目前主要证据来自摘要和站点元数据，不能视为对全文技术细节的完整复原。"],
            "topic_relevance": f"该论文命中了当前配置的“{self._topic_label()}”主题词，初步判断与该主题相关。",
            "repo_assessment": ["仓库关联性还需要人工进一步核验。"],
            "follow_up_questions": ["建议打开论文 PDF，补充公式、实验表格和关键实现细节。"],
            "reading_priority": "medium"
        }

    def _render_paper_markdown(self, summary: PaperSummary) -> str:
        repo_lines = [
            f"- [{repo.name}]({repo.url}) | stars={repo.stars} | updated={repo.updated_at}\n  - {repo.description}"
            for repo in summary.github_repos
        ]
        module_lines = []
        for module in summary.key_modules:
            name = module.get("name", "模块")
            role = module.get("role", "")
            input_text = module.get("input", "")
            output_text = module.get("output", "")
            details = module.get("details", "")
            module_lines.append(f"- {name}")
            if role:
                module_lines.append(f"  - 作用：{role}")
            if input_text:
                module_lines.append(f"  - 输入：{input_text}")
            if output_text:
                module_lines.append(f"  - 输出：{output_text}")
            if details:
                module_lines.append(f"  - 机制：{details}")
        if not module_lines:
            module_lines = ["- 当前证据不足，无法可靠拆出关键模块。"]
        equation_lines = []
        for eq in summary.core_equations:
            equation_lines.append(f"- {eq.get('name', '公式')}: `{eq.get('formula', '') or '未提供'}`")
            if eq.get("meaning"):
                equation_lines.append(f"  - 含义：{eq.get('meaning')}")
        if not equation_lines:
            equation_lines = ["- 当前收集到的证据中没有明确公式"]

        def bullet_list(items: list[str], empty: str) -> str:
            if not items:
                return f"- {empty}"
            return "\n".join(f"- {item}" for item in items)

        return "\n".join(
            [
                f"# {summary.title}",
                "",
                "## 论文信息",
                f"- 来源：{summary.source}",
                f"- 论文链接：{summary.paper_url or '无'}",
                f"- PDF 链接：{summary.pdf_url or '无'}",
                f"- 发布时间：{summary.published or '无'}",
                f"- 更新时间：{summary.updated or '无'}",
                f"- 阅读优先级：{summary.reading_priority}",
                f"- 作者：{', '.join(summary.authors) if summary.authors else '无'}",
                "",
                "## 一句话总结",
                summary.tldr or "无",
                "",
                "## 研究问题",
                summary.problem_statement or "无",
                "",
                "## 研究缺口",
                summary.research_gap or "无",
                "",
                "## 核心假设",
                summary.central_hypothesis or "无",
                "",
                "## 整体方案总览",
                summary.system_overview or "无",
                "",
                "## 创新点",
                bullet_list(summary.innovation_points, "无"),
                "",
                "## 方法拆解",
                bullet_list(summary.method_breakdown, "无"),
                "",
                "## 关键模块拆解",
                "\n".join(module_lines),
                "",
                "## 实现流水线",
                bullet_list(summary.implementation_pipeline, "无"),
                "",
                "## 训练与优化",
                bullet_list(summary.training_or_optimization, "无"),
                "",
                "## 推理流程",
                bullet_list(summary.inference_workflow, "无"),
                "",
                "## 为什么它有效",
                bullet_list(summary.why_it_works, "无"),
                "",
                "## 核心公式",
                "\n".join(equation_lines),
                "",
                "## 实验与结果",
                bullet_list(summary.experiments_and_results, "无"),
                "",
                "## 局限性",
                bullet_list(summary.limitations, "无"),
                "",
                "## 技术启发",
                bullet_list(summary.technical_takeaways, "无"),
                "",
                "## 证据质量与推断边界",
                bullet_list(summary.evidence_quality, "无"),
                "",
                f"## 与“{self._topic_label()}”的关系",
                summary.topic_relevance or "无",
                "",
                "## 相关 GitHub 仓库",
                "\n".join(repo_lines) if repo_lines else "- 当前没有收集到可确认相关的仓库",
                "",
                "## 仓库评估",
                bullet_list(summary.repo_assessment, "无"),
                "",
                "## 后续关注问题",
                bullet_list(summary.follow_up_questions, "无"),
                "",
                "## 检索证据",
                "### 摘要",
                summary.abstract or "无",
                "",
                "### Google Scholar 摘录",
                summary.scholar_snippet or "无",
                "",
                "### Scholar 元信息",
                summary.scholar_meta or "无",
            ]
        ).strip() + "\n"

    def _render_daily_index(
        self, summaries: list[PaperSummary], repo_radar: list[RepoCandidate], reason: str
    ) -> str:
        paper_lines = []
        for idx, summary in enumerate(summaries, start=1):
            paper_lines.extend(
                [
                    f"## {idx}. {summary.title}",
                    f"- 阅读优先级：{summary.reading_priority}",
                    f"- 论文链接：{summary.paper_url or '无'}",
                    f"- 摘要速览：{summary.tldr}",
                    f"- 核心机制：{summary.system_overview or '无'}",
                    f"- 主题相关性：{summary.topic_relevance}",
                    "",
                ]
            )
        repo_lines = [
            f"- [{repo.name}]({repo.url}) | stars={repo.stars} | updated={repo.updated_at} | {repo.description}"
            for repo in repo_radar
        ]
        paper_section = paper_lines if paper_lines else ["- 今天没有生成论文摘要"]
        repo_section = repo_lines if repo_lines else ["- 今天没有收集到 GitHub 仓库线索"]
        return "\n".join(
            [
                f"# {self._topic_label()} 每日报告 - {self._today_str()}",
                "",
                f"- 触发原因：{self._format_reason(reason)}",
                f"- 当前主题：{self._topic_label()}",
                f"- 监控 QQ：{', '.join(self._get_watched_user_ids()) or '(未设置)'}",
                f"- 空闲触发小时数：{self._cfg('runtime.inactivity_hours', 12)}",
                f"- 启用来源：{', '.join(self._enabled_source_labels()) or '(未启用)'}",
                f"- 实际生成论文数：{len(summaries)}",
                f"- GitHub 雷达数：{len(repo_radar)}",
                "",
                "## 论文简报",
                *paper_section,
                "",
                "## GitHub 雷达",
                *repo_section,
            ]
        ).strip() + "\n"

    def _build_manifest(
        self,
        reason: str,
        candidates: list[PaperCandidate],
        summaries: list[PaperSummary],
        repo_radar: list[RepoCandidate],
        generated_files: list[str],
    ) -> dict[str, Any]:
        return {
            "plugin_name": PLUGIN_NAME,
            "plugin_version": PLUGIN_VERSION,
            "generated_at": datetime.now().isoformat(),
            "reason": reason,
            "reason_label": self._format_reason(reason),
            "topic_label": self._topic_label(),
            "focus_queries": self._focus_queries(),
            "enabled_sources": self._enabled_source_labels(),
            "stats": {
                "candidate_count": len(candidates),
                "paper_count": len(summaries),
                "repo_radar_count": len(repo_radar),
                "generated_file_count": len(generated_files),
            },
            "generated_files": generated_files,
            "papers": [asdict(summary) for summary in summaries],
            "repo_radar": [asdict(repo) for repo in repo_radar],
        }

    def _get_summary_provider(self):
        provider_id = str(self._cfg("providers.summary_provider_id", "")).strip()
        provider = None
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        if not provider:
            provider = self.context.get_using_provider()
        return provider

    def _get_watched_user_ids(self) -> list[str]:
        configured = self._cfg("runtime.watched_user_ids", [])
        watched = [str(x).strip() for x in configured if str(x).strip()]
        if watched:
            return watched
        return list(self.admin_ids)

    def _focus_queries(self) -> list[str]:
        configured = self._cfg("research.focus_queries", [])
        values = [str(x).strip() for x in configured if str(x).strip()]
        return values or [
            "artificial intelligence",
            "machine learning",
            "multimodal model",
            "agent",
        ]

    def _topic_label(self) -> str:
        label = str(self._cfg("research.topic_label", "")).strip()
        return label or "研究主题"

    def _coerce_modules(self, value: Any) -> list[dict[str, str]]:
        modules: list[dict[str, str]] = []
        if not isinstance(value, list):
            return modules
        for item in value:
            if isinstance(item, dict):
                modules.append(
                    {
                        "name": self._safe_text(item.get("name")),
                        "role": self._safe_text(item.get("role")),
                        "input": self._safe_text(item.get("input")),
                        "output": self._safe_text(item.get("output")),
                        "details": self._safe_text(item.get("details")),
                    }
                )
            elif isinstance(item, str) and item.strip():
                modules.append(
                    {
                        "name": item.strip(),
                        "role": "",
                        "input": "",
                        "output": "",
                        "details": "",
                    }
                )
        return modules

    def _notification_platform_id(self) -> str:
        configured = str(self._cfg("notifications.platform_id", "")).strip()
        if configured:
            return configured
        for inst in self.context.platform_manager.platform_insts:
            try:
                meta = inst.meta()
                if meta and meta.id:
                    return meta.id
            except Exception:
                continue
        return ""

    def _notify_targets(self) -> list[str]:
        configured = self._cfg("notifications.notify_session_ids", [])
        values = [str(x).strip() for x in configured if str(x).strip()]
        if not values:
            values = self._get_watched_user_ids()
        platform_id = self._notification_platform_id()
        sessions: list[str] = []
        for value in values:
            if ":" in value:
                sessions.append(value)
            elif platform_id:
                sessions.append(f"{platform_id}:FriendMessage:{value}")
        return list(dict.fromkeys(sessions))

    def _scheduled_datetime_today(self, raw_time: str) -> datetime | None:
        try:
            hour_str, minute_str = raw_time.split(":", 1)
            now = datetime.now()
            return now.replace(
                hour=int(hour_str),
                minute=int(minute_str),
                second=0,
                microsecond=0,
            )
        except Exception:
            return None

    def _build_notification_text(
        self,
        reason: str,
        summaries: list[PaperSummary],
        repo_radar: list[RepoCandidate],
        generated_files: list[str],
        kb_message: str,
    ) -> str:
        top_lines = []
        for idx, summary in enumerate(summaries[:3], start=1):
            top_lines.append(f"{idx}. {summary.title}")
        desktop_index = next((path for path in generated_files if path.endswith("/_index.md")), "")
        lines = [
            f"主题：{self._topic_label()}",
            f"触发原因：{self._format_reason(reason)}",
            f"生成论文数：{len(summaries)}",
            f"GitHub 雷达数：{len(repo_radar)}",
            f"知识库结果：{kb_message}",
        ]
        if top_lines:
            lines.append("今日论文：")
            lines.extend(top_lines)
        if desktop_index:
            lines.append(f"索引文件：{desktop_index}")
        return "\n".join(lines)

    async def _notify_result(self, title: str, body: str, notify_key: str) -> None:
        notify_enabled = self._cfg("notifications.enabled", True)
        if not notify_enabled:
            self.state["last_notification_status"] = "disabled"
            return

        if notify_key == "success" and not self._cfg("notifications.notify_on_success", True):
            self.state["last_notification_status"] = "skipped_success"
            return
        if notify_key == "empty" and not self._cfg("notifications.notify_on_empty", False):
            self.state["last_notification_status"] = "skipped_empty"
            return
        if notify_key == "failure" and not self._cfg("notifications.notify_on_failure", True):
            self.state["last_notification_status"] = "skipped_failure"
            return

        session_ids = self._notify_targets()
        if not session_ids:
            self.state["last_notification_status"] = "no_target"
            return

        chain = MessageChain().message(f"{title}\n{body}")
        sent = 0
        for session_id in session_ids:
            try:
                await self.context.send_message(session_id, chain)
                sent += 1
            except Exception as exc:
                logger.warning("[%s] notify %s failed: %s", PLUGIN_NAME, session_id, exc)
        self.state["last_notification_status"] = f"sent:{sent}/{len(session_ids)}"

    def _enabled_source_labels(self) -> list[str]:
        return [
            label
            for key, label in [
                ("research.enable_arxiv", "arXiv"),
                ("research.enable_google_scholar", "Google Scholar"),
                ("research.enable_github", "GitHub"),
            ]
            if self._cfg(key, True)
        ]

    def _resolve_output_dir(self) -> Path:
        configured = self._cfg("outputs.desktop_output_dir", DEFAULT_DESKTOP_DIR)
        return Path(str(configured)).expanduser()

    def _kb_import_dir(self) -> Path:
        configured = self._cfg(
            "outputs.knowledge_base_import_subdir",
            f"imports/{DEFAULT_COLLECTION}",
        )
        return Path(StarTools.get_data_dir("astrbot_plugin_knowledge_base")) / str(configured)

    @staticmethod
    def _format_reason(reason: str) -> str:
        mapping = {
            "manual": "手动触发",
            "idle": "空闲自动触发",
            "startup": "启动后自动首跑",
            "daily_time": "固定时刻触发",
            "retry_failure": "失败后补跑",
            "retry_empty": "空结果补跑",
            "scheduled": "自动触发",
        }
        return mapping.get(str(reason).strip(), str(reason).strip())

    def _score_candidate(self, candidate: PaperCandidate) -> float:
        score = 0.0
        title_lower = candidate.title.lower()
        abstract_lower = candidate.abstract.lower()
        for query in self._focus_queries():
            q = query.lower()
            if q in title_lower:
                score += 3.0
            if q in abstract_lower:
                score += 1.5
        if "arxiv" in candidate.source:
            score += 2.0
        if candidate.scholar_snippet:
            score += 0.5
        if candidate.updated:
            score += 0.5
        return score

    def _record_run(
        self, status: str, reason: str, message: str, generated_files: list[str]
    ) -> None:
        self.state["last_run_date"] = self._today_str()
        self.state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        self.state["last_run_reason"] = reason
        self.state["last_run_status"] = status
        self.state["last_error"] = "" if status == "success" else message
        self.state["last_generated_files"] = generated_files
        history = self.state.get("run_history", [])
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "at": self.state["last_run_at"],
                "reason": reason,
                "status": status,
                "message": message,
            }
        )
        self.state["run_history"] = history[-20:]
        self._save_state()

    def _save_state(self) -> None:
        self.state_file.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.state.update(saved)
        except Exception as exc:
            logger.warning("[%s] failed to load state: %s", PLUGIN_NAME, exc)

    def _today_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _state_datetime(self, key: str) -> datetime | None:
        raw = str(self.state.get(key, "") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _cfg(self, path: str, default: Any = None) -> Any:
        current: Any = self.config
        for part in str(path).split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id() or "").strip() in self.admin_ids

    def _require_http(self) -> httpx.AsyncClient:
        if not self.http:
            raise RuntimeError("HTTP client is not initialized.")
        return self.http

    @staticmethod
    def _clean_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _strip_html(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", text or "")
        return re.sub(r"\s+", " ", unescape(text)).strip()

    @staticmethod
    def _normalize_title(title: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", title.lower())

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
        return slug[:96] or "paper"

    @staticmethod
    def _safe_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @staticmethod
    def _coerce_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def _coerce_equations(value: Any) -> list[dict[str, str]]:
        equations: list[dict[str, str]] = []
        if not isinstance(value, list):
            return equations
        for item in value:
            if not isinstance(item, dict):
                continue
            equations.append(
                {
                    "name": str(item.get("name", "")).strip(),
                    "formula": str(item.get("formula", "")).strip(),
                    "meaning": str(item.get("meaning", "")).strip(),
                }
            )
        return equations

    @staticmethod
    def _extract_json(text: str) -> Optional[dict[str, Any]]:
        if not text:
            return None
        candidate = text
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _github_query_for_paper(title: str) -> str:
        core = " ".join(title.split()[:8]).strip()
        return f'"{core}"'
