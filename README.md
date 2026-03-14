# astrbot_plugin_research_digest

一个可泛化主题的 AstrBot 论文巡检插件。

它会在你指定的 QQ 长时间没有主动找 Bot 时，自动去检索论文与相关仓库，生成系统化、规范化的中文摘要，并把结果同时写到桌面目录和 AstrBot 知识库。

## 适用场景

- 每天自动追踪某个研究方向的新论文
- 为角色 Bot 持续补充外部研究知识
- 把论文摘要整理成稳定的 Markdown 档案
- 需要一个可换主题、可换 Prompt、可接知识库的通用研究插件

## 当前能力

- 支持按主题关键词检索 `arXiv`、`Google Scholar`、`GitHub`
- 支持“用户空闲后自动触发”与管理员手动触发
- 支持在 AstrBot 插件设置页中单独配置主题、Prompt、每日数量、来源开关
- 每篇论文单独输出一个 `.md`
- 每天生成一个 `_index.md` 和可选的 `manifest.json`
- 可把摘要镜像到知识库目录，并写入 AstrBot knowledge base collection

## 命令

- `/digest run`
  立即执行一次论文巡检
- `/digest status`
  查看当前主题、来源开关、最近运行状态
- `/digest prompt`
  查看当前实际生效的总结 Prompt

兼容别名：

- `/embodied ...`
- `/paper ...`
- `/research ...`

## 核心配置

插件设置页里建议优先看这些字段：

- `research.topic_label`
  主题名称，例如“具身智能”“扩散模型”“多模态大模型”
- `research.focus_queries`
  真实用于搜索的关键词列表
- `research.max_papers_per_run`
  每日生成多少篇完整论文摘要
- `runtime.watched_user_ids`
  监控哪些 QQ 号的“主动消息”
- `outputs.sync_to_knowledge_base`
  是否同步写入 AstrBot 知识库
- `prompts.summary_prompt_override`
  完整覆盖默认总结 Prompt
- `prompts.summary_prompt_prefix`
  给默认 Prompt 加前置身份或风格
- `prompts.daily_focus_note`
  每天额外关注的研究重点
- `prompts.summary_prompt_suffix`
  末尾补充要求

## 输出结构

默认桌面目录：

- `~/Desktop/Research_Paper_Summaries/YYYY-MM-DD/`

典型产物：

- `01-paper-title.md`
- `02-paper-title.md`
- `_index.md`
- `manifest.json`

单篇论文 Markdown 默认包含这些部分：

- 论文信息
- 一句话总结
- 研究问题
- 创新点
- 方法拆解
- 核心公式
- 实验与结果
- 局限性
- 与当前主题的关系
- 相关 GitHub 仓库
- 仓库评估
- 后续关注问题
- 检索证据

## 已知限制

- `Google Scholar` 抓取属于 best-effort，可能因为反爬限制返回空结果或 `403`
- 数学公式只会在已有证据足够时写入，不会强行补全
- 如果未安装或未初始化 `astrbot_plugin_knowledge_base`，插件仍会生成 Markdown，但会跳过知识库写入
