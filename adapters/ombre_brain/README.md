# 在本机给 Ombre-Brain 接入联想召回

适用于 Ombre-Brain **3.6.14**，核对的提交为 `115d831d979bf67efc7150f976e2cb34c333c935`。适配器从桶的 Markdown 正文取证，联想核心负责生成线索、检索、版本复核与选择记录。原有 OB 服务可以照常使用，安装位置在独立的召回包中。[上游提交](https://github.com/P0luz/Ombre-Brain/tree/115d831d979bf67efc7150f976e2cb34c333c935)

## 先跑离线示例

在含 `assoc_index.py` 与 `service.py` 的包根目录执行。以下命令适用于 macOS 或 Linux，已在 Python 3.12 上验证。

```sh
python -m pip install -r requirements.txt
python -B -m adapters.ombre_brain.demo
python -B -m unittest discover -s adapters/ombre_brain/tests -v
```

示例 vault 有两条合成记录。应只返回工作台上的琥珀灯，结果包含 `event_id`、逐字引文 `excerpt`、正文窗口 `source_window` 和 `evidence_version`。静默桶及 frontmatter 中另存的引语都不应出现。示例模型只为固定句子提供线索和核验答复。SQLite 索引、检索和交付前的 `validate` 均使用包内真实核心，无需模型账户或 embedding 服务。

`local_recall` 每次在 vault 外创建独立临时状态目录，退出时结束后台任务并清理目录。测试在目录权限 0555、文件权限 0444 的副本上运行，逐项比较运行前后的文件内容、权限和修改时间。

## OB 的公开接口与本地数据口

公开 MCP 工具 `breath()` 用于默认浮现。`breath_search(query=..., mode="automatic", with_ids=True)` 表明此次检索由宿主自动发起。普通搜索响应可在末尾附加 `=== ombre:result-ids ===` 和 schema 1 的 JSON，字段包括 `mode`、`bucket_ids`、`count`、`omitted_by_policy`。按完整 ID 读取走单独分支，可以直接返回渲染文本。结果清单不能充当枚举接口、持续授权或版本凭据。Dashboard 的 `GET /api/buckets` 与 `GET /api/bucket/{bucket_id}` 使用人类侧权限。这个适配器通过本机文件接入，不会向这些端点发请求。[工具注册](https://github.com/P0luz/Ombre-Brain/blob/115d831/src/server.py) · [检索分支](https://github.com/P0luz/Ombre-Brain/blob/115d831/src/tools/breath/search.py) · [Dashboard 路由](https://github.com/P0luz/Ombre-Brain/blob/115d831/src/web/buckets.py)

传入 OB 配置里的 `buckets_dir`。上游 writer 在类型目录和主题域目录下写入 UTF-8 Markdown，YAML frontmatter 存放 `id`、`type`、`domain`、`created` 等元数据，分隔符后的正文才可用作证据。适配器接受 `dynamic/*.md` 和 `dynamic/<主题域>/*.md`。文件名可以修改，事件 ID 取自头部，每个桶投影为一个 `<id>:body` 片段。[桶写入逻辑](https://github.com/P0luz/Ombre-Brain/blob/115d831/src/bucket_manager.py)

| 核心调用 | 本适配器提供的内容 |
| --- | --- |
| `vault.moments.list_all()` / `.get(moment_id)` | 同步读取现有可见正文与稳定片段 ID |
| `await vault.list_all(include_archive=True)` / `await vault.get(id)` | 当前授权桶、日期、内容指纹和合成来源键。归档还须满足宿主授权条件 |
| `vault.superseded_ids()` | 同步比较本实例曾见过的桶来源键与当前可见集合，返回已经撤回的键。重启后仍由桶和片段端口复核旧候选 |
| `vault.list_edges()` | 同步读取宿主当前确认的 `updates` 关系，只返回两个端点均可见的关系 |

异步宿主可直接等待 `vault.get(bucket_id)`、`vault.list_all(include_archive=False)`、`vault.moments.get_async(moment_id)`、`vault.moments.list_all_async(limit=None)`、`vault.superseded_ids_async()` 和 `vault.list_edges_async()`。每次调用在工作线程读取一次策略，校验通过后执行一次原有扫描流程。有归档授权时，还会扫描归档目录。`read_policy` 需支持从多个线程调用；使用 SQLite 的宿主可在回调内打开连接。

### 哪些记录会进入索引

默认选择未解决的普通 `dynamic` 桶。类型、目录、来源工具、标签、主题域和可见性标记一起检查。专用通道、信件锁标记、静默、已消化、置顶、锚点、受保护、删除和墓碑数据均排除。上游的手动检索、常驻准则与专用通道各有自己的可见范围。这里使用更小的自动联想范围。未知头部字段、重复键或别名、无法判读的来源、可解析桶之间的 ID 冲突、非普通文件、软硬链接及超过 1 MiB 的桶会被排除。正常标签和日期可以保留，私有标记会使整桶退出。[上游可见性策略](https://github.com/P0luz/Ombre-Brain/blob/115d831/src/ombrebrain/policy/surfacing.py) · [信件识别](https://github.com/P0luz/Ombre-Brain/blob/115d831/src/ombrebrain/storage/letter_lock.py)

OB 3.0 起已移除四个公开的原文回读与绑定工具，包括 `source_read`。这里始终不打开 `_sources/*.source`，也不跟随正文链接、媒体路径或 Markdown 里的指令。`quotes`、`meaning`、`media` 与关系提示不会混入引文。窗口里的话仍属于原记录中的发言者，宿主展示时应保留上下文。[当前原文层说明](https://github.com/P0luz/Ombre-Brain/blob/115d831/docs/INTERNALS.md#331-原文证据层--只写不读300)

旧桶可只有 `source_refs`。新桶的 `source_links` 记录绑定状态，两份字段同在时应对得上。只要一个绑定为 `detached`、两份投影冲突或行范围格式异常，该桶全部退出。共享同一 `src_<sha256>` 的另一个桶按自身绑定判断。核心使用的整数来源键只标识本集合里的桶；OB 没有向这个接口提供逐条原始对话的撤回编号。无法证明哪些句子仍然有效时，这里不会做局部恢复。[来源绑定格式](https://github.com/P0luz/Ombre-Brain/blob/115d831/src/ombrebrain/storage/source_store.py)

## 接持久状态、已授权归档和明确的后续记录

**接入真实笔记库时，建议通过 `read_policy` 读取最新授权清单，明确哪些笔记可以参与自动召回。** 已配置的清单暂时读不到时，本轮跳过召回并保留索引；离线示例无需清单。

宿主为每个记忆集合和受众准备一个独占、可信的状态目录，放在 vault 外。由宿主在每次调用 `read_policy()` 时提供该受众**此刻**获准用于自动联想的桶，以及作者或管理者明确建立的更新关系。这个策略由宿主维护，不属于 OB 的 Markdown 格式。示例可从宿主原子替换的 JSON 文件读取。

```python
import json
from pathlib import Path

from adapters.ombre_brain import OmbreVault, host_recall


async def validate_for_turn(buckets_dir, policy_path, state_dir, model, embeddings, request):
    policy_path = Path(policy_path)
    vault = OmbreVault(buckets_dir, read_policy=lambda: json.loads(policy_path.read_text("utf-8")))
    async with host_recall(vault, state_dir=state_dir, model=model, embeddings=embeddings) as (_, recall):
        return await recall.validate(request)
```

长驻宿主在启动时进入一次 `host_recall`，在停机时退出。它使用包根目录的 `service.open_service`，返回完整的 `index, recall`。首次接入可调用 `await index.rebuild(limit=...)` 分批建索引，检查 `failed` 和 `complete`；立即查询前可用 `await index.warm()` 刷新内存索引。宿主可连接自己的线索模型、判断模型和向量客户端。交付时把真实会话作用域、候选 ID、版本和逐字引文送入 `validate`，使用本次返回的窗口。宿主继续负责真实对话中的 `prepare/query/finish` 与提交时序。

```json
{
  "dynamic_ids": ["repair"],
  "archives": {
    "workshop/repair.md": {
      "id": "record",
      "original_type": "dynamic",
      "sha256": "<宿主审核过的该归档 Markdown 文件的完整 SHA-256>"
    }
  },
  "updates": [["repair", "record"]]
}
```

这个例子授权一条当前事件和一份归档正文。归档路径相对于 `archive/`。宿主确认该记录原属普通事件、当前受众获准在本用途读取**正文**，再按审核过的文件字节签发授权。适配器同时核对路径、ID、原类型声明、摘要、当前 `type: archived` 和其余可见性字段。编辑归档会使旧授权失效。

省略 `read_policy` 时，适配器只接普通动态桶。配置了回调时，每次读取都需要一份完整策略。必填字段为 `dynamic_ids`；可选的 `archives` 和 `updates` 缺省为空。宿主应原子发布整份策略，每次回调返回同一时点的快照，暂时无法确认权限时抛出异常。适配器为每次调用单独复制并校验这份快照；并发读取和同一次重建中的不同端口调用都可以看到不同版本。

策略文件缺失、内容为空、解析失败，或策略含有未知字段、格式错误的归档授权或更新关系时，桶和片段的 `get` 返回 `None`。两个 `list_all`、`list_edges` 和 `superseded_ids` 抛出 `adapters.ombre_brain.vault.PolicyUnavailable`。全量重建遇到此异常会在清理索引前退出，宿主可在恢复后重试。已有候选在策略恢复后仍需通过当前授权和版本校验。宿主确认该受众已无任何授权时，应返回 `{"dynamic_ids": []}`；此时全量重建会正常清理已撤权的索引记录。

更新关系的两个 ID 分别对应原事件与后续记录。宿主从正式记录取得这对 ID；OB 自动推断的 `relation_links`、共同标签、相似度和创建时间都不足以授权这条边。关系参与证据版本计算，撤边后旧的跨桶结论立即失效。核心仍要核验后来记录的逐字证据。没有可验证的结果时保留 `unknown`。测试 `test_durable_authorized_archive_and_authored_update` 包含可运行的完整流程，覆盖写入索引、复核完成状态、关闭后重新打开同一 SQLite 状态、撤边以及归档文件变化。

## 更新与验证边界

每次读取都重新打开当前文件。正文、绑定或其他文件字节变化会改变证据指纹，单纯更新激活计数也会触发重新索引。宿主策略使用同样的即时复核。旧候选在下一次校验时失效；此前已交付的消息、进程外缓存和已经发送给模型的文字由宿主按自己的留存规则管理。持久 SQLite 可保存引文和生成线索，访问权限与清理周期应匹配原集合。扫描针对小型本地库，多次核验会重复扫描动态桶；当前实现没有为并发写入提供跨文件事务快照。使用离线副本时，撤回可见性的时点取决于副本何时更新。

升级 OB 时可先在合成库重跑下列真实 writer 验证。将最后一个参数换成本地上游 Git 仓库路径。脚本固定提取上述提交的 `src` 和 `VERSION`，在测试目录下临时解包，结束后清理。上游依赖只需装在测试环境中。

```sh
python -m pip install python-frontmatter==1.3.0 rapidfuzz==3.14.5
python -B -m adapters.ombre_brain.tests.upstream_writer /path/to/upstream.git
```

**已测到上游存储层与真实召回核心。** 测试实际调用 `SourceStore.put` 写入合成隐藏原文，再调用 `BucketManager.create` 生成带来源范围和引语的事件桶、另生成一封永久锁定信件。适配器只交付事件正文，读前读后的 vault 内容和修改时间一致。随后上游 `update` 修改正文、设置及解除静默，`archive` 移动桶，旧候选按当前状态被拒绝。整个测试使用本地固定模型答复，没有外部模型调用。验证范围到此为止，未运行 OB 的 MCP/HTTP 服务、鉴权流程、真实聊天前端或生产 vault。

适配器可独立替换。升级时先核对候选版本的工具签名、归档与信件识别、绑定账本、writer 字段及核心端口，再在测试副本中更新固定提交并运行两组验证。新版新增字段导致记录消失时，先确定其可见性语义，随后扩充字段表和回归样例。
