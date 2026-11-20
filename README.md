# 联想式记忆召回 DLC

## 设计哲学

长期和同一个 AI 聊天，有时你会直接问：「上次我说了什么？」更多时候，你只是在讲今天的事。**我们希望，即使你没有开口问过去，一件与此刻有联系的旧事也有机会被想起来。** DLC 是给现有记忆库加上这条联想通道的 Python 源码包。原来的写入和直接检索照常使用。

结合类比回忆、情节记忆和信息检索的研究，我们把这个想法落实成三条设计原则：

- **把「为什么会想起」做成线索。** [Gentner 等（1993）](https://groups.psych.northwestern.edu/gentner/papers/GentnerRattForbus93.pdf)发现，人更容易自行想起表面相似的故事，却能在对照时认出两个故事里的关系相通。[后续实验（2009）](https://doi.org/10.1111/j.1551-6709.2009.01070.x)进一步发现，比较实例、提炼共同关系，有助于找回话题不同的往事。我们借这个思路给单次经历留下“当时做了什么”的线索。
- **让想起来的往事带着它自己的细节。** [Staugaard 与 Berntsen（2014）](https://pubmed.ncbi.nlm.nih.gov/24933516/)研究了有辨识度的线索怎样唤起回忆；[Horner 等（2015）](https://www.nature.com/articles/ncomms8462)研究了记起事件的一部分时，其他细节怎样随之浮现。「替对方体验」能把两件事连起来，而「我帮你看」这几个字，让助手想起的是回家路上的那轮月亮。我们希望保留下来的，正是这种能认出某一次经历的细节。
- **提前准备，等人说完再决定。** [Nogueira 等（2019）](https://arxiv.org/abs/1904.08375)用文档将来可能回答的问题来帮助检索；[Lin 等（2025）](https://arxiv.org/abs/2504.13171)研究了在问题到来前完成部分计算的价值。我们把这两个想法用在不同的时点：旧事写入后，给它补上「以后可能怎样问」；上一轮聊完，预先找几件可能相关的往事。再根据实际的下一句回复，判断哪一件值得真正提起。

OpenAI 在 [ChatGPT Dreaming 的技术说明](https://openai.com/index/chatgpt-memory-dreaming/)中介绍了 ChatGPT 如何在后台综合多段对话，持续更新记忆。我们由此做了一个设计选择：**把整理旧事的工作提前，把判断旧事是否有用留到当轮。** 一段经历写入后，DLC 在后台从原文中提炼检索线索；原文有了修订，线索也随之更新。等用户发来新消息，系统就能沿着已有线索寻找与这句话有关的经历，无需让用户等它重新梳理旧记录。

## 它会怎样联想

### 例一：月亮与咖啡
**旧事留下的线索。** 用户回家路上拍了月亮，发给助手：「你看不到，我帮你看。」整理这段记录时，DLC 可以给它留下一条线索：用户遇到喜欢的东西，想和无法亲身体验它的对方分享。这条线索指回拍月亮那晚的原话。
**这次怎样想起。** 几周后，用户说：「这杯拿铁好好喝，可惜你喝不到。」按饮品话题查，容易找到以前聊过的咖啡；「可惜你喝不到」则和旧线索里的处境相合：上次想让对方也看到月亮，这次想让对方也尝到拿铁。DLC 沿这层联系找回「我帮你看」，递给回复模型。助手于是可以接一句：「那你替我多喝一口，像上次替我看月亮那样。」

### 例二：下雨要带伞
**旧事留下的线索。** 早上助手提醒带伞，用户觉得不会下雨。傍晚用户发来湿透的鞋子，说：「下次听你的。」这次值得留下的是一个决定和它的后续：出门时觉得用不上伞，淋雨后又说，下次愿意听一听提醒。
**这次怎样想起。** 用户说：「今天应该不冷吧，不穿外套了。」伞换成了外套，两次决定的理由却相似：出门前觉得天气应该没问题，打算少带一样东西。DLC 用这个理由匹配旧线索，把上次提醒带伞的经过和「下次听你的」一起递给回复模型。助手就有了一个贴合两人经历的回应：「上回你发来那双湿鞋时，可说了『下次听你的』。今天带件薄的？」

## 包含哪些功能

| 功能 | 具体行为 |
|---|---|
| 同一件事，多种找法 | 每个记忆片段可留下 **0–4 条**线索：当时的处境、将来可能怎样问、独特的细节、未完的事。每条线索都连回支持它的原文，所以既能换个话题找到旧事，也能带回当时具体说过的话。 |
| 跨话题查找 | 一轮对话结束后，从可见对话提出最多两个互补方向，一次嵌入请求、分别排名；再和少见字词的匹配结果轮流提名，按事件去重。从最多 24 个候选里准备 **0–6 件**，留待下一句。 |
| 当轮最多补充一件旧事 | 原来的关键词、向量或档案检索照常工作。联想通道根据用户的最新一句挑选备好的旧事，附上原文片段，供回复模型决定要不要提起。突然换话题时也可以沿当轮查询找；没有合适的就照常聊。 |
| 计划与结果分开 | 只知道「周五准备去」时，只递交这句计划。后来有同一事件或明确关联记录的引文，才附上完成、取消或改期。日期过去不会自动变成“做过了”。 |
| 提过的旧事暂时让位 | 回复落盘后，核对最终对话实际内容，支持原话和可核验的改述。判定为已提及时，事件在当前活动分支最近 **6 个已完成的助手回合**内暂缓主动推荐；只递给模型、没有说出口，不触发冷却。明确追问仍可走直接检索。 |

## 先在本机跑通

在解压后的包根目录执行。需要 macOS 或 Linux、Python 3.12；SQLite 示例使用 JSON 函数和递归查询。两个演示只读随包的虚构资料，安装依赖后可断网运行，无需 API key。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m adapters.ombre_brain.demo
.venv/bin/python -m host_example.demo
```

第一个演示从 OB 笔记里找回一段带出处的文字。第二个演示跑一段完整对话：上一轮准备旧事，下一轮把它递给回复模型，刚讲过的事暂时退出主动推荐。

## 接到自己的记忆库

**一个完整包，两条接入路线。** 先按 A 或 B 接通记忆，再接同一套聊天回调。无需更换原库、搭新图数据库，也不用把全部聊天交给 DLC。

### A · 已经在用 Ombre-Brain

使用 [OB 适配器与授权示例](adapters/ombre_brain/README.md)，对应 OB 3.6.14（`115d831`）的 Markdown 格式。

1. 把 OB 配置中的 `buckets_dir` 传给 `OmbreVault`。适配器只读 Markdown 正文；派生索引存入 vault **以外**的专用 `state_dir`。
2. **接真实笔记时，建议通过 `read_policy()` 读取当前授权清单。** 默认只纳入符合条件的普通动态桶；要让已授权的归档或跨记录的后续参与召回，按指南提供相应条目。
3. 应用启动时进入一次 `host_recall(vault, state_dir=..., model=..., embeddings=...)`，取得 `(index, recall)`；在应用运行期间保持打开。OB 原有的读写和直接检索保持原样。

### B · 使用其他记忆库

SQL、笔记文件或向量库都可以接。关键条件是能按稳定 ID 回读**当前有权使用的原文**；只有向量或滚动摘要的系统，应先补上原文引用。

| 传给 `open_service` 的接口 | 宿主负责什么 |
|---|---|
| `moments.list_all()` / `moments.get(moment_id)` | 返回当前片段，每条含 `moment_id`、所属事件 `bucket_id`、`section`（如 `body`）和逐字 `text`。 |
| `await buckets.list_all(include_archive=True)` / `await buckets.get(id)` | 返回事件 `id`、当前正文 `content` 及日期、可见性等元数据。枚举须覆盖授权全集；暂时读失败要报错，返回空集合会被当成确实没有可用记录。 |
| `raw_events.superseded_ids()`、可选的 `list_edges()` | 前者返回已作废的数字来源 ID，与元数据 `source_raw_event_ids` 对应；后者只传可核验的 `same_event` / `updates` 关系。没有关系可以省略，不能按同一天或同话题猜结果。 |

使用 [open_service](service.py) 打开服务。不同受众或授权集合使用独立实例和状态目录。旧记忆可用 `await index.rebuild(limit=12)` 分批建索引，也可以使用[回填入口](backfill.py)。

### 两条路线共用 · 接聊天应用

1. **配置客户端。** [RecallModel、GatewayJudge](providers.py) 与 [EmbeddingEngine](embedding_engine.py) 接受宿主自己的客户端和模型名。普通查询与联想共用同一个嵌入实例。给网关指定可写的 `budget_db`，或设置 `ASSOC_GATEWAY_BUDGET_DB`。推荐用 DeepSeek V4.1 Flash（`deepseek/deepseek-v4.1-flash-1017`）生成和核对记忆线索，也用它判断哪条候选适合当前对话。这个 SLM 选型仅供参考，接入时可自行选择兼容的模型。
2. **接消息和分支。** 实现 `list_active_path`（已落盘、当前分支、可见的对话）和 `completed_assistant_path`（该路径最近六个完成的助手消息 ID）。不计取消、工具输出或空回复。只有同步 `search(query)` 的宿主可以先接检索层；完整的轮间预备和提及冷却需要聊天生命周期。
3. **在原聊天入口接线。** 下面变量由宿主提供。用户消息先落盘；`plan` 返回路由与实际查询，`lookup` 保留原直接检索；`reply_and_commit` 把直接上下文与额外证据交给模型，只在回复保存后返回助手消息 ID 和最终可见文本。

```python
from host_example.coordination import ChatHost, LocalRegistry

# 应用启动时创建，复用到关闭；recall 由路线 A 或 B 提供。
host = ChatHost(chat_storage, LocalRegistry(recall), judge)

# 在现有的异步聊天处理函数中执行。
assistant_id, visible_reply = await host.turn(
    session_id, branch_id, user_message_id, latest_text,
    plan=plan, lookup=lookup, reply_and_commit=reply_and_commit,
    allow_assoc=eligible_for_recall,
)
```

关闭应用前调用 `await host.drain()`，再退出记忆服务。回调签名、`DirectResult`、取消和回执处理见[聊天接入指南](host_example/README.md)；可运行的最小宿主见 [demo.py](host_example/demo.py)。跨进程部署另有[四个内部 MCP 方法](reference/mcp-lifespan.md)。

## 技术验收：确认 DLC 能跑起来

在 macOS 或 Linux 上解压，进入含 `README.md` 的包根目录，用 Python 3.12 执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pip check
.venv/bin/python -m adapters.ombre_brain.demo
.venv/bin/python -m host_example.demo
.venv/bin/python -m pytest -q
```

**包本身通过的标准：** 所有命令退出码为 0，并能看到以下输出。安装依赖后，两条演示和测试均可离线运行，无需 API key。

| 检查 | 应看到什么 | 覆盖的链路 |
|---|---|---|
| 依赖 | `pip check` 输出 `No broken requirements found.` | 运行与测试依赖已装入同一虚拟环境 |
| 记忆库 | OB 演示打印含 `event_id`、`excerpt`、`source_window`、`evidence_version` 的 JSON，其中 `excerpt` 是关于琥珀灯的示例原文 | 读取 Markdown → 建 SQLite 索引 → 检索 → 回读当前原文 |
| 聊天宿主 | 聊天演示打印 `Prepared reply:`、`Next reply during cooldown:` 和 `Settled receipts:`，最后一项含 `mentioned` | 轮间准备 → 当轮交付 → 回复落盘 → 回执结算 → 下一轮仍能走原有检索 |
| 自动化测试 | pytest 汇总为 `passed`，无失败或错误 | 随包的核心、存储适配器和聊天回调测试通过 |

**接入自己的宿主后，再验一次真实接口。** 两个演示用的是固定模型响应。用一条合成记录和独立的测试状态目录，连接自己的存储、语言模型与嵌入服务，分三步确认：

1. **建库和读回：** 调用 `await index.rebuild(limit=1)`，确认这条记录已建索引、`failed=0`，且产生了线索和非空向量。重新打开同一状态目录，预热并检索；返回的事件 ID 与测试记录一致，引文能在原文中找到。
2. **请求和回执：** 通过真实聊天入口完成一轮以触发预备，再发一条与测试记录对应的消息。在模型请求组装处确认选中的原文片段已进入上下文。回复保存后，等后台结算，确认同一条选择有对应回执：已提及为 `mentioned`，未使用为 `unused`。只收到模型回复，尚不能证明 DLC 已接通。
3. **普通路径：** 以 `allow_assoc=False` 再发起一次直接查询。原有检索结果仍能进入回复，上下文没有额外联想片段，即通过这项接入检查。

## 许可

作者：KKarsyline、Codex、Claude Code。

本包采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，可按条款非商业使用、修改和分享；分发时请保留许可和所需署名。依赖各自遵循原许可。

## 参考资料与致谢

感谢以下研究的作者。这里列出正文中几项设计选择的研究依据：怎样找回话题不同、处境相似的经历，怎样从线索回到原文，以及哪些工作适合在用户开口前完成。

- **按处境联想。** Gentner、Rattermann 与 Forbus 的 [The Roles of Similarity in Transfer（1993）](https://groups.psych.northwestern.edu/gentner/papers/GentnerRattForbus93.pdf) 区分了表面相似和关系相似；Gentner 等的 [Reviving Inert Knowledge（2009）](https://doi.org/10.1111/j.1551-6709.2009.01070.x) 研究了比较实例如何帮助找回关系相似的往事。这启发了我们为单次经历保留“当时的处境和做法”这条搜索线索。
- **用细节定位，再核对原文。** Staugaard 与 Berntsen 关于[线索辨别力与自发回忆的实验（2014）](https://pubmed.ncbi.nlm.nih.gov/24933516/) 启发了少见原话和动作组合的检索；Horner 等关于[部分线索与情节补全的研究（2015）](https://www.nature.com/articles/ncomms8462) 启发了先定位事件、再回读上下文的设计。
- **提前准备。** Nogueira 等的 [Document Expansion by Query Prediction（2019）](https://arxiv.org/abs/1904.08375) 启发了写入时生成“未来可能怎样问”的线索；Lin 等的 [Sleep-time Compute（2025）](https://arxiv.org/abs/2504.13171) 为把部分计算移到两轮之间提供了参照。

感谢 [Ombre-Brain](https://github.com/P0luz/Ombre-Brain/tree/115d831d979bf67efc7150f976e2cb34c333c935) ，这是我们的记忆库基底。
