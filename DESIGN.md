# 快照元数据局部更新设计（DESIGN）

本轮只交付设计，不修改任何生产源码、回归、样例与观察器；`lake.py`、`catalog.py`、`objects.py`、`checks.py`、`replay.py`、`observe.py`、`design_check.py`、`examples/` 全部保持原样。本文所有行号均针对仓库当前源码。

## 1. 现状调用链与写放大定位

- 业务入口 `Lake`（lake.py:5）持有 `Catalog` 与 `Objects`（lake.py:6-8）。载荷由不可变行文件承载：`Objects.put` 规范化 JSON 字节并以独占方式写 `fid.json`（objects.py:13-28），`Objects.get` 按文件名读回（objects.py:30-32）；同名同内容合法、异名内容冲突报 `IMMUTABLE_CONFLICT`（objects.py:22-26），这层不在本题改造范围。
- 元数据现状是三张按快照全量复制的表：`files(sid,fid)`、`tombstones(sid,fid,pos)` 带指向同快照文件的外键、`head` 单行（catalog.py:10-17），快照 0 与初始 head 在建库时插入（catalog.py:18-19）。
- 写事务语义：`Catalog.write` 最外层 `BEGIN IMMEDIATE`，嵌套调用加入外层事务，异常逃出最外层时 `ROLLBACK`（catalog.py:22-34）。`Catalog.commit` 强制要求处于事务内（catalog.py:49-50），取当前 head 加一作为新 sid（catalog.py:51-53），随后把**整份排序文件集合**与**整份排序墓碑集合**复制进新快照（catalog.py:54-56），再单行更新 head（catalog.py:57）。
- 各入口的放大点：
  - `Lake.seed` 在事务内对全部输入建快照 1（lake.py:10-16），全量输入在边界内被允许。
  - `Lake.delete` 先在事务外读源快照做 `BAD_POSITION` 校验（lake.py:28-30），事务内再读当前快照做 `STALE_LAYOUT` 与重复墓碑判断（lake.py:31-36），任何一次实际删除都经 `commit` 复制全部 files 与 tombstones（lake.py:37）。
  - `Lake.prepare` 经 `Catalog.state` 取整份 files/dead（lake.py:40，对应 catalog.py:39-46 的两个全表 `WHERE sid=?` 查询），再只对 k 个输入取载荷（lake.py:43）；墓碑过滤发生在 lake.py:51。
  - `Lake.publish` 在事务内重读整份状态、先比 `base` 再比输入子集（lake.py:57-63），提交时对“去掉 k 个输入、加入 g 个输出”后的**整个**新集合与过滤后的**整个**墓碑集合做全量插入（lake.py:64-66）。
  - `Lake.read` 枚举目标快照的整份 files 与 dead（lake.py:19，catalog.py:43-45），按文件名排序后逐文件取载荷并按墓碑成员过滤（lake.py:21-24）。
  - `Lake.rewrite` 是同一写事务内的 prepare/publish 封装（lake.py:68-71），锁范围本轮不动。
- 观察器实测印证了放大：`sparse` 每次单行删除都写 8 行 files、墓碑行 1/2 递增，重复删除为 0 写入，回滚探针出现过 8 行 files 与 3 行 tombstones 的尝试写入；`local` 两次发布分别写 9 行、8 行 files（见 observe.py:145-159 的断言与本文第 9 节证据）。计量口径是 SQL 交给调用者的行数与 DML 影响行数，不是页 I/O 或虚拟机访问数（observe.py:40-60），本文预测沿用同一“逻辑记录/条目”口径。

## 2. 必须保持的语义与设计不变量

1. 外部接口、返回结构、快照编号、错误名与错误优先级不变：`NO_SNAPSHOT` → `BAD_POSITION`（lake.py:28-30 先于事务）→ `STALE_LAYOUT`（lake.py:33-34）→ 重复墓碑直接返回 head（lake.py:35-36）；`BAD_INPUTS` 先于 `BAD_LAYOUT`（lake.py:41-47）；`PUBLISH_CONFLICT` 先于 `BAD_PLAN`（lake.py:59-63）；`ALREADY_SEEDED`（lake.py:14-15）。
2. 快照与 head 仍由 SQLite 单事务原子提交，保留 `BEGIN IMMEDIATE`/嵌套加入/异常回滚（catalog.py:22-34）；载荷不在元数据事务内，外层回滚可留下不可达对象但不影响历史读取（catalog.py:20 所述现状保持，载荷回收不在本轮）。
3. 历史快照永久保留、不可变；历史读取只能沿目标快照自己的文件、墓碑与必要索引路径，禁止逐代重放差分，禁止扫描全部历史版本后按 sid 筛选。
4. 单行删除与局部重写不得枚举或复制整份文件集合/全体墓碑；允许复制有界索引路径节点，节点内附带条目计入成本，禁止把整份清单改名为单个索引节点。
5. 文件名永久唯一不复用、同名同内容载荷合法（objects.py:14-28）；零行文件仍是合法文件条目；空 layout 与零输出发布合法。
6. 不实现、不迁移、不回收载荷、不做分支与多维护者协调；不要求旧位置删除穿过重写接续。

## 3. 候选存储策略比较

### 3.1 策略 A：当前态可变表 + 增量日志 + 周期检查点

结构：`cur_files(fid)`、`cur_tombstones(fid,pos)` 为只描述 head 的可变行；`changelog(sid,op,fid,pos)` 只追加每次删除/重写的增量；每 C 代把当前整份清单物化进 `checkpoint(sid,...)`。

- 写：单行删除只插 1 行 changelog（加 1 行当前态更新）；发布写 k+g+d_in 量级日志，写放大确实消除。
- 读历史：先找不大于目标 sid 的最近检查点，再重放其后至多 C 条差分。无论 C 取多大，这都是**逐代重放差分**；C 有限时每次检查点又重新复制整份清单，C 无限时退化为重放全部 H 代。
- 结论：写路径满足边界，但历史读直接违反第 2.3 条硬边界（CONTRACT.md 第 51 行），淘汰。

### 3.2 策略 B（推荐）：不可变 copy-on-write B-tree 清单，根指针绑定快照

结构：文件与墓碑各是一棵只追加、从不更新的 COW B-tree；`snapshots` 行保存两棵树的根节点 id。任何修改只沿键路径复制有界节点、写入新节点，旧节点被旧快照根继续引用；树节点表没有 sid 列，结构上不可能做“按 sid 扫全历史”。

- 写：单行删除只新增一条根路径（h+1 个节点以内）；发布只新增 k、g、d_in 涉及的键路径节点。
- 读历史：对目标 sid 主键点查一次拿到根，随后只遍历该根可达的节点；与 H 无关，不重放 parent 链。
- 结论：满足全部硬边界，选用。详细结构见第 4 节。

### 3.3 策略 C：全历史修订表（起止 sid 区间）

结构：`file_revisions(fid, created_sid, removed_sid NULLABLE)`、`tomb_revisions(fid,pos,created_sid,removed_sid)`，读快照 s 用 `created_sid<=s AND (removed_sid IS NULL OR removed_sid>s)` 过滤。

- 写是 O(k+g+d_in) 级局部插入，看似满足；但任何历史读都要扫描两张修订表的**全部历史行**再按 sid 区间筛选，正是第 2.3 条禁止的“扫描全部历史版本后按 sid 筛选”；随 H 增长读取成本无界。淘汰。

### 3.4 比较矩阵

| 维度 | A 增量日志+检查点 | B COW B-tree（推荐） | C 全历史修订表 |
|---|---|---|---|
| 单行删除写入 | O(1) 日志 | O(h_D+2) 新节点，与 F 无关 | O(1) |
| 局部重写写入 | O(k+g+d_in) 日志 | O((h+1)(k+g+d_in+1)) 新节点 | O(k+g+d_in) |
| 历史读是否重放差分 | 是，至多 C 代（违规） | 否，根指针直达 | 否，但全表 sid 过滤（违规） |
| 是否存在全历史扫描 | 检查点物化整单 | 不可能：节点表无 sid，仅根可达 | 是，结构性存在 |
| 额外存储 | 日志加周期性整单副本 | 初始树 + 每次写的有界 COW 路径 | 每条修订全局一行，随 H 累积 |
| 排序枚举（lake.py:21） | 检查点内排序、重放后重排 | 树中序即 fid 序，天然有序 | 需要额外排序 |
| 结论 | 淘汰 | 采用 | 淘汰 |

B 的变体（radix/hash trie）边界同阶；选 B-tree 是因为中序枚举正好对应 lake.py:21 的 `sorted(files)`，且每步下降都是 SQLite 主键点查，与现有单机 SQLite 栈一致。

## 4. 推荐结构（策略 B）

### 4.1 表结构

```sql
CREATE TABLE snapshots(
    sid INTEGER PRIMARY KEY,
    parent INTEGER,
    file_root INTEGER,          -- 文件树根节点 id；空树为 NULL
    dead_root INTEGER           -- 墓碑树根节点 id；空树为 NULL
);
CREATE TABLE tree_nodes(
    node_id INTEGER PRIMARY KEY,            -- 普通 INTEGER PRIMARY KEY，回滚后编号可复用
    kind TEXT NOT NULL CHECK(kind IN ('leaf','internal')),
    lo TEXT, hi TEXT,                       -- 该节点负责任何键的闭区间，便于审计与裁剪
    entries TEXT NOT NULL,                  -- JSON 数组：叶为 [key,payload]，内部节点为分隔键
    child_ids TEXT NOT NULL                 -- JSON 数组：叶为 []，内部为子节点 id
);
CREATE TABLE head(singleton INTEGER PRIMARY KEY CHECK(singleton=1), sid INTEGER);
-- 初始化仍插入 snapshots(0,NULL,NULL,NULL) 与 head(1,0)
```

- 文件树条目：键为 fid 本身，载荷为文件标记（行数不进元数据，行数仍由 `Objects.get` 决定，见 lake.py:22、objects.py:30-32）。
- 墓碑树条目：键为 `json.dumps([fid,pos], separators=(',',':'))` 的紧凑字节串，同族编码自定序；点查用同一编码。墓碑不再带 catalog.py:14-16 那样的 SQL 外键，“墓碑必属于同快照现存文件”的不变量改由 `commit_delta` 从同一 base 根同时施加两棵树的编辑、以及 lake.py:28-36 的既有校验保证。
- 节点不可变：除事务内尚未提交的新行外，从不 `UPDATE`/`DELETE` `tree_nodes`；修改一律插入新 node_id。旧快照根引用的节点链永久可达。
- 节点容量参数 M：叶节点至多 M 个条目，内部节点至多 M 个子指针（M-1 个分隔键）。树高（边数）`h(N)=0 当 N<=M，否则 ceil(log_M N)`。本文样例预测取 **M=64**：F≤9、D≤2 时全部为单叶根，h=0。M 只影响扇出与单节点附带条目数，不影响渐近边界。

### 4.2 替换端口（Catalog 内部，Lake 外部签名不变）

| 现有端口 | 替换端口 | Lake 调用点 |
|---|---|---|
| `Catalog.head`（catalog.py:36-37） | 同名同 SQL，不变 | lake.py:14、32 经 state 间接、59 |
| `Catalog.state`（catalog.py:39-46） | 拆为 `resolve_roots(sid)`、`files_contains(root,fid)`、`dead_contains(root,fid,pos)`、`enumerate_files(root)` | lake.py:19、28、32、40、58 |
| `Catalog.commit`（catalog.py:48-58） | `commit_delta(file_edits, dead_edits)`，仍强制事务内、head+1、原子绑定两根 | lake.py:16、37、64-66 |
| `Catalog.write`（catalog.py:22-34） | 语义与 SQL 完全保留 | lake.py:13、31、57、69 |

`Lake` 与 `Objects` 的方法名、参数、错误串不变；本轮不改源码，下表与第 5 节伪代码描述目标端口行为。

## 5. 事务伪代码

`?` 为绑定参数；所有树访问都是 `tree_nodes` 主键点查或从某根出发的有界下降/深度遍历，不存在带 sid 的扫描。

### 5.1 基础只读原语

```text
resolve_roots(sid):
    row = SELECT file_root, dead_root FROM snapshots WHERE sid = ?
    if row is None: raise ValueError("NO_SNAPSHOT")
    return row

point_get(root, key):
    if root is NULL: return None
    node = SELECT kind, entries, child_ids FROM tree_nodes WHERE node_id = ? (root)
    while node.kind = 'internal':                      # 至多 h 次主键点查
        child = 按分隔键选择 child_ids 中唯一下行子节点
        node = SELECT ... FROM tree_nodes WHERE node_id = ? (child)
    return node.entries 中按键探测的载荷（叶内 ≤M 条，逐条计入访问成本）

enumerate_files(root):
    从 root 深度优先、按 child_ids 顺序访问该树全部节点（仅该根可达节点），
    中序收集叶内 fid；访问的节点数等于该快照文件树自身节点数，条目数等于 F_s
```

### 5.2 COW 写入原语

```text
cow_apply(base_root, edits):
    # edits 为 ('insert',key,payload) / ('erase',key) / ('erase_prefix',keyprefix)
    # 1) 沿每个被编辑键从 base_root 下降，复制路径上的内部节点（INSERT 新 node_id）
    # 2) 在叶副本上施加编辑：insert 合并后超 M 则分裂并向上传播新内部节点；
    #    erase/erase_prefix 合并后条目为 0 的叶不保留，父节点相应摘除，整树清空则返回 NULL
    # 3) 未被编辑的子树 child_id 原样共享，不读取、不复制其节点
    # 4) 全部新节点在当前事务内 INSERT；base_root 指向的旧节点一行不改
    return new_root
```

### 5.3 seed（允许全量建索引，对应 lake.py:10-16）

```text
seed(files):
    for fid, rows in files.items(): objects.put(fid, rows)      # 同 lake.py:11-12
    with catalog.write():                                        # BEGIN IMMEDIATE
        if SELECT sid FROM head WHERE singleton=1 != 0:
            raise ALREADY_SEEDED
        fr = 按排序 fid 批量装包（每叶 M 条）并自底向上建内部节点，INSERT 全部节点
        dr = NULL                                                # 空墓碑树
        INSERT INTO snapshots(sid,parent,file_root,dead_root) VALUES(1,0,fr,dr)
        UPDATE head SET sid=1 WHERE singleton=1
        COMMIT
```

### 5.4 delete（对应 lake.py:27-37，错误顺序保持）

```text
delete(snapshot, fid, pos):
    roots = resolve_roots(snapshot)                             # 不存在 -> NO_SNAPSHOT
    if files_contains(roots.file_root, fid) is False
       or type(pos) is not int
       or not (0 <= pos < len(objects.get(fid))):
        raise BAD_POSITION                                      # 同 lake.py:29-30，事务外
    with catalog.write():                                       # BEGIN IMMEDIATE
        cur = resolve_roots(SELECT sid FROM head WHERE singleton=1)
        if files_contains(cur.file_root, fid) is False:
            raise STALE_LAYOUT                                  # 同 lake.py:33-34
        if dead_contains(cur.dead_root, fid, pos):
            return head_sid()                                   # 同 lake.py:35-36：零 DML
        new_dead = cow_apply(cur.dead_root, [('insert', enc(fid,pos), marker)])
        sid = head_sid() + 1
        INSERT INTO snapshots VALUES(sid, head_sid(), cur.file_root, new_dead)
        UPDATE head SET sid=sid WHERE singleton=1
        COMMIT
        return sid
    # 任何异常逃出最外层 -> catalog.py:31-34 的 ROLLBACK：新节点、snapshots、head 全部撤销
```

### 5.5 prepare（无写事务，对应 lake.py:39-54）

```text
prepare(job, inputs, layout):
    roots = resolve_roots(head_sid())
    if len(inputs) != len(set(inputs)) or not inputs
       or any(files_contains(roots.file_root, f) is False for f in inputs):
        raise BAD_INPUTS                                        # 同 lake.py:41-42
    payload = {f: objects.get(f) for f in inputs}               # 同 lake.py:43，载荷口径不变
    expected / requested 去重与全覆盖检查不变（lake.py:44-47），违例 -> BAD_LAYOUT
    for i, group in enumerate(layout):
        fid = "rewrite-" + job + "-" + str(i)
        rows = [payload[f][p] for (f,p) in group
                if not dead_contains(roots.dead_root, f, p)]    # 点查替代整单 dead 枚举
        objects.put(fid, rows)                                  # 零行组同样写载荷
    return {"base": head_sid(), "inputs": sorted(inputs), "outputs": outputs}
```

### 5.6 publish（对应 lake.py:56-66，冲突与校验顺序保持）

```text
publish(plan):
    with catalog.write():                                       # BEGIN IMMEDIATE
        sid = head_sid()
        if sid != plan["base"]: raise PUBLISH_CONFLICT          # 同 lake.py:59-60
        roots = resolve_roots(sid)
        inputs = set(plan["inputs"])
        if any(files_contains(roots.file_root, f) is False for f in inputs):
            raise BAD_PLAN                                      # 同 lake.py:61-63
        file_edits = [('erase', f) for f in inputs]
                    + [('insert', o, marker) for o in plan["outputs"]]
        dead_edits = [('erase_prefix', enc_prefix(f)) for f in inputs]
        new_fr = cow_apply(roots.file_root, file_edits)
        new_dr = cow_apply(roots.dead_root, dead_edits)         # 移除输入的当前墓碑
        new_sid = sid + 1
        INSERT INTO snapshots VALUES(new_sid, sid, new_fr, new_dr)
        UPDATE head SET sid=new_sid WHERE singleton=1
        COMMIT
        return new_sid
    # 异常 -> ROLLBACK；plan 不合法时树原语根本不执行
```

### 5.7 read（对应 lake.py:18-25）

```text
read(snapshot=None):
    sid = head_sid() if snapshot is None else snapshot
    roots = resolve_roots(sid)                                  # 一次主键点查，与 H 无关
    rows = []
    for fid in enumerate_files(roots.file_root):                # 仅遍历该快照自己的文件树
        for pos, value in enumerate(objects.get(fid)):
            if not dead_contains(roots.dead_root, fid, pos):    # NULL 根短路，否则键路径点查
                rows.append({"file": fid, "pos": pos, "value": value})
    return {"snapshot": sid, "rows": rows}
```

`parent` 列仅保留编号与审计用途，任何读取路径都不沿 parent 回溯。

## 6. 历史一致性论证

1. **不可变可达**：节点只插不改；每次提交把新两根绑定到新 snapshots 行，旧行的根仍指向旧节点链。对 sid=s 的读取与并发新提交互不干扰，SQLite 单事务保证读者只看到“旧 head+旧根”或“新 head+新根”，不存在悬空头。
2. **无需重放差分**：目标快照的文件/墓碑集合由其两根一次性闭合描述，`parent` 不参与读取；策略 A 的代际重放在结构上不存在。
3. **无全历史扫描**：`tree_nodes` 没有 sid 列，访问方式只有根出发的 DFS 与键路径主键点查；其他快照不可达的节点不会被触碰，策略 C 的 sid 过滤扫描在结构上无法表达。
4. **结果同形**：`enumerate_files` 的中序即 fid 升序，与 lake.py:21 的 `sorted(files)` 同序；墓碑点查复刻 lake.py:23 的成员过滤；值仍来自 objects.py:30-32 的同一载荷，因此返回行与现状逐行相同。
5. **证据中的历史输出可达性核对**：`sparse` 三次 read.result 为 16/15/14 行——sid=1 的 dead_root 为 NULL 全活；sid=2 根链含 1 条 `[f00,0]`；sid=3 根链含 2 条墓碑，各次只遍历本快照根。`local` 四次 read.result 为 16/15/15/15——sid=2 的 dead 叶保留 `[f00,0]`；sid=3 的文件树根含 9 个条目（含零行 `rewrite-local-1`，载荷为 `[]` 故不产生行）、dead_root 已为 NULL；sid=4 文件树 8 个条目。旧快照的根与节点在发布后均未被修改，历史输出永久可复现。

## 7. 读写与额外存储上界

记号：F=当前文件数，D=当前墓碑数，H=历史快照数，k=本次涉及输入文件数，g=本次输出文件数，d=本次涉及墓碑数，d_in=其中属于重写输入的墓碑（d_in≤d），F_s、D_s=目标快照 s 自身的文件/墓碑数，R_s=该快照存活行数，M=节点容量；h_F=h(F)、h_D=h(D)，h=max(h_F,h_D)。节点内附带条目按实际读取逐条计入，单节点不超过 M。

### 7.1 元数据读上界（交给调用方的行/条目）

| 操作 | 基线现状 | 方案 B 上界 |
|---|---|---|
| delete 的源快照校验 | 整单 F_s+D_s（catalog.py:43-45） | 两条键路径：O((h_F+1)+(h_D+1)) 节点，节点内条目 ≤M(h+2) |
| delete 事务内当前态 | 再读整单 F+D | 同上，O(M·h) |
| prepare 全部校验 | 整单 F+D（lake.py:40） | k 次文件点查 + P 次墓碑点查（P=输入物理位置总数）：O(k(h_F+1)+P(h_D+1)) |
| publish 冲突/计划校验 | 整单 F+D（lake.py:58） | 1 行 head + k 次文件点查：O(k(h_F+1)) |
| read(s) | 整单 F_s+D_s | 1 行 snapshots + 文件树自身全部节点（F_s 条目）+ R_s 次墓碑键路径：O(F_s + R_s·h_D)；**不含 H 项** |
| 重复 delete | 两次整单读 | 命中即止的两条路径，O(M·h)，零 DML |

### 7.2 元数据写上界（DML 影响行）

| 操作 | 基线现状 | 方案 B 上界 |
|---|---|---|
| seed | F_1+D_1 行（允许全量） | 建树节点 O(F_1) 行 + 1 snapshots + 1 head |
| 单行删除（新墓碑） | F+D 行全量复制 | 墓碑树新节点 ≤ h_D+2（空树首叶算 1），文件树 0，加 snapshots/head 各 1 |
| publish | (F-k+g)+(D-d_in) 行 | 文件树新节点 ≤ (h_F+1)(k+g+1)；墓碑树新节点 ≤ (h_D+1)(d_in+1)；加 2 |
| 重复删除 | 0 | 0 |
| 外层回滚 | 尝试写入后全撤销 | 同上尝试量，COMMIT 前 ROLLBACK，持久化 0；普通 INTEGER PRIMARY KEY 编号回滚后可复用 |

### 7.3 额外存储上界

- 基线：H 行 snapshots、1 行 head、Σ_s(F_s+D_s) 行清单，即 O(H(F+D))。
- 方案 B：widened snapshots H 行、head 1 行；`tree_nodes` 总行数 ≤ (F_1+D_1) + Σ_t (h_t+1)(k_t+g_t+d_t+2)，t 遍历各次写操作。稀疏维护下为 O((F+D) + W·h·(k+d))（W 为写次数）；若每次写都触碰全量，最坏退化到与基线同阶 O(H(F+D))，不会更差，而单行/局部场景从 O(F+D) 降到 O(h·(k+d))。
- 载荷存储口径不变，不可达对象回收不在本轮。

## 8. 场景核对

1. **大量无关文件或历史**：删除一个 fid 只下降 h+1 个节点，与 F 无关；新写 COF 出的新节点不影响旧根，历史读成本只含 F_s、D_s，H 只表现为 snapshots 主键点查的常数代价。
2. **同文件多次稀疏删除**：同 fid 的不同 pos 反复命中同一条叶路径，每次只重写该叶及 h 个内部祖先；不枚举任何其他文件。同 pos 再删则命中第 8.3 条。
3. **重复请求**：`dead_contains` 命中即按 lake.py:35-36 返回当前 head，事务内零 DML；`sparse` 第 3 步实测 write 全 0，方案预测同为 0。
4. **移除带墓碑输入**：publish 的 `erase_prefix` 只下降覆盖该 fid 前缀的叶（d_in 条墓碑所在路径），`local` 首次发布后当前 `dead=[]`、dead_root=NULL；被清空的叶不保留，而 sid=2 的旧 dead 叶仍被旧根引用，历史 15 行读数不变。
5. **空文件与空组**：零行输出照常 `objects.put(fid, [])`（lake.py:52）并在文件树插入条目，故 sid=3 有 9 个文件条目但只读 15 行；空 layout 对应 g=0，发布只做 erase，文件叶 9→8（`local` 第 5 步）。
6. **无输出发布**：`outputs` 为空集时 lake.py:64-66 的集合并差运算仍成立，方案 B 只写删除路径节点，snapshots/head 正常递增，语义与返回快照号不变。
7. **外层回滚**：回滚探针在最外层事务内抛错，catalog.py:31-34 触发 ROLLBACK，COW 新节点、snapshots 行、head 更新全部撤销；观察器记录的 8 files/3 tombstones 是尝试量而非持久量，方案 B 下 head 与两根原样保留，逻辑状态回退到提交前。
8. **staged 冲突语义**：`PUBLISH_CONFLICT` 仍由事务内 `head != base` 判定（lake.py:59-60），`STALE_LAYOUT` 仍由当前文件树点查判定（lake.py:33-34）；`replay.py all` 的 between/after 时序结论保持，无需让旧位置删除穿过重写。

## 9. 基线实测与设计预测核对（M=64，样例 h 均为 0）

观察器口径（observe.py:40-60）：read 统计游标交付行数，write 统计 DML 影响行数；下列“基线实测”取自 `observe.run` 原样输出，“方案预测”是方案 B 在同一逻辑轨迹下的逻辑记录数，供人工审查（CONTRACT.md 第 68 行：预测计数不预设自动校验）。`design_check.py` 自动核对的是预测 logical 与实测 logical 逐布相等。

### 9.1 sparse（8 文件各 2 行）

| 步 | 操作 | 基线实测 write（snapshots/files/tombstones/head） | 方案 B 预测持久写入 | 预测访问 |
|---|---|---|---|---|
| 1 | delete f00:0 → sid2 | 1/8/1/0 | dead 叶 1（空树首叶）+快照 1+head 1=3 | 源根叶 1 + 当前根叶 1，均 ≤8 条目 |
| 2 | delete f01:0 → sid3 | 1/8/2/1 | dead 叶重写 1（2 条目）+2=3 | 同上，不触其余 7 文件 |
| 3 | 重复 delete f00:0 | 0/0/0/0 | 0 | dead 叶点查命中即返回 head |
| 4 | rollback_delete f02:0 | 尝试 1/8/3/1，持久 0 | 尝试 dead 叶 1+快照 1+head 1，ROLLBACK 后 0 | 回滚后旧根链不变，head=3 |
| 5-7 | read sid 1/2/3 | 只读，files=8/8/8，tombstones=0/1/2 | 0 写；读 1 snapshots 行 + 各快照自己的 1 文件叶 + 0/1/1 个 dead 叶 | 16/15/14 行，根指针直达 |
| 8 | delete 越界 pos99 | 0 写，BAD_POSITION | 0 写，事务前拒绝 | 源文件叶点查 + 载荷长度 2 |

### 9.2 local（删后重写、零行文件、空 layout）

| 步 | 操作 | 基线实测 write | 方案 B 预测持久写入 | 预测访问 |
|---|---|---|---|---|
| 1 | delete f00:0 → sid2 | 1/8/1/0 | dead 叶 1+2=3 | 两条根叶路径 |
| 2 | prepare k=1（含空组） | 0 | 0 | 文件点查 1 叶 + dead 点查 1 叶；载荷写 2 个对象 |
| 3 | publish → sid3 | 1/9/0/1 | 文件叶重写 1（8→9 条目）+快照 1+head 1=3；dead 叶清空不保留、新 dead_root=NULL | k=1 点查 + 两条 COW 路径 |
| 4 | prepare 零行文件空 layout | 0 | 0 | 单文件点查 1 叶；dead_root=NULL 短路 |
| 5 | publish 无输出 → sid4 | 1/8/0/1 | 文件叶重写 1（9→8）+2=3 | 仅 erase 路径 |
| 6-9 | read sid 1..4 | files=8/8/9/8，tombstones=0/1/0/0 | 0 写；每快照 1 snapshots 行 + 自己的文件叶（+sid2 的 dead 叶） | 16/15/15/15 行，旧根不可变 |

对照基线断言（observe.py:145-159）：sparse 写 files 序列 8,8,0,8、tombstones 1,2,0,3、head 2,3,3,3；local 两次发布 files 写 9、8 且首次发布后 dead 为空——方案 B 在保持全部 logical 状态相等的前提下，把每步元数据写入从整单 F+D 降为表中有界的路径节点数。

## 10. 证据重放入口与交付边界

- 交付后检查入口为 `python3 design_check.py`：它先跑 `observe.verify()`（内含 check() 全量业务回归），再重放 `examples/metadata.json`，核对下文证据块中的 actual 可复现、predicted 与 actual 等长且 logical 逐布相等、access 非空，并校验本文引用的源码文件名与行号有效（design_check.py:7-27）。访问边界、成本与事务伪代码按 CONTRACT.md 第 71 行保留人工审查。
- 本文证据块的 actual 由 `observe.run` 生成、未手工改写；read.result 保留完整历史行值，predicted 只给 logical 与访问解释，不把目标结构写成可运行代码。
- 明确不做：目标结构实现、旧库迁移、载荷回收、分支、多维护者协调、新恢复框架、旧位置删除跨重写接续、随机单行读与行文件格式改造。

## 11. 元数据证据

<!-- metadata-evidence -->
```json
{
  "sparse": {
    "actual": {
      "initial": {
        "head": 1,
        "files": [
          "f00",
          "f01",
          "f02",
          "f03",
          "f04",
          "f05",
          "f06",
          "f07"
        ],
        "dead": []
      },
      "steps": [
        {
          "op": "delete",
          "snapshot": 2,
          "cost": {
            "read": {
              "snapshots": 2,
              "files": 16,
              "tombstones": 0,
              "head": 2
            },
            "write": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 1,
              "head": 1
            }
          },
          "logical": {
            "head": 2,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ]
            ]
          }
        },
        {
          "op": "delete",
          "snapshot": 3,
          "cost": {
            "read": {
              "snapshots": 2,
              "files": 16,
              "tombstones": 1,
              "head": 2
            },
            "write": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 2,
              "head": 1
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ],
              [
                "f01",
                0
              ]
            ]
          }
        },
        {
          "op": "delete",
          "snapshot": 3,
          "cost": {
            "read": {
              "snapshots": 2,
              "files": 16,
              "tombstones": 2,
              "head": 1
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ],
              [
                "f01",
                0
              ]
            ]
          }
        },
        {
          "op": "rollback_delete",
          "error": "OBSERVER_ROLLBACK",
          "cost": {
            "read": {
              "snapshots": 2,
              "files": 16,
              "tombstones": 2,
              "head": 2
            },
            "write": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 3,
              "head": 1
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ],
              [
                "f01",
                0
              ]
            ]
          }
        },
        {
          "op": "read",
          "result": {
            "snapshot": 1,
            "rows": [
              {
                "file": "f00",
                "pos": 0,
                "value": {
                  "reading": 0
                }
              },
              {
                "file": "f00",
                "pos": 1,
                "value": {
                  "reading": 1
                }
              },
              {
                "file": "f01",
                "pos": 0,
                "value": {
                  "reading": 2
                }
              },
              {
                "file": "f01",
                "pos": 1,
                "value": {
                  "reading": 3
                }
              },
              {
                "file": "f02",
                "pos": 0,
                "value": {
                  "reading": 4
                }
              },
              {
                "file": "f02",
                "pos": 1,
                "value": {
                  "reading": 5
                }
              },
              {
                "file": "f03",
                "pos": 0,
                "value": {
                  "reading": 6
                }
              },
              {
                "file": "f03",
                "pos": 1,
                "value": {
                  "reading": 7
                }
              },
              {
                "file": "f04",
                "pos": 0,
                "value": {
                  "reading": 8
                }
              },
              {
                "file": "f04",
                "pos": 1,
                "value": {
                  "reading": 9
                }
              },
              {
                "file": "f05",
                "pos": 0,
                "value": {
                  "reading": 10
                }
              },
              {
                "file": "f05",
                "pos": 1,
                "value": {
                  "reading": 11
                }
              },
              {
                "file": "f06",
                "pos": 0,
                "value": {
                  "reading": 12
                }
              },
              {
                "file": "f06",
                "pos": 1,
                "value": {
                  "reading": 13
                }
              },
              {
                "file": "f07",
                "pos": 0,
                "value": {
                  "reading": 14
                }
              },
              {
                "file": "f07",
                "pos": 1,
                "value": {
                  "reading": 15
                }
              }
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 0,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ],
              [
                "f01",
                0
              ]
            ]
          }
        },
        {
          "op": "read",
          "result": {
            "snapshot": 2,
            "rows": [
              {
                "file": "f00",
                "pos": 1,
                "value": {
                  "reading": 1
                }
              },
              {
                "file": "f01",
                "pos": 0,
                "value": {
                  "reading": 2
                }
              },
              {
                "file": "f01",
                "pos": 1,
                "value": {
                  "reading": 3
                }
              },
              {
                "file": "f02",
                "pos": 0,
                "value": {
                  "reading": 4
                }
              },
              {
                "file": "f02",
                "pos": 1,
                "value": {
                  "reading": 5
                }
              },
              {
                "file": "f03",
                "pos": 0,
                "value": {
                  "reading": 6
                }
              },
              {
                "file": "f03",
                "pos": 1,
                "value": {
                  "reading": 7
                }
              },
              {
                "file": "f04",
                "pos": 0,
                "value": {
                  "reading": 8
                }
              },
              {
                "file": "f04",
                "pos": 1,
                "value": {
                  "reading": 9
                }
              },
              {
                "file": "f05",
                "pos": 0,
                "value": {
                  "reading": 10
                }
              },
              {
                "file": "f05",
                "pos": 1,
                "value": {
                  "reading": 11
                }
              },
              {
                "file": "f06",
                "pos": 0,
                "value": {
                  "reading": 12
                }
              },
              {
                "file": "f06",
                "pos": 1,
                "value": {
                  "reading": 13
                }
              },
              {
                "file": "f07",
                "pos": 0,
                "value": {
                  "reading": 14
                }
              },
              {
                "file": "f07",
                "pos": 1,
                "value": {
                  "reading": 15
                }
              }
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 1,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ],
              [
                "f01",
                0
              ]
            ]
          }
        },
        {
          "op": "read",
          "result": {
            "snapshot": 3,
            "rows": [
              {
                "file": "f00",
                "pos": 1,
                "value": {
                  "reading": 1
                }
              },
              {
                "file": "f01",
                "pos": 1,
                "value": {
                  "reading": 3
                }
              },
              {
                "file": "f02",
                "pos": 0,
                "value": {
                  "reading": 4
                }
              },
              {
                "file": "f02",
                "pos": 1,
                "value": {
                  "reading": 5
                }
              },
              {
                "file": "f03",
                "pos": 0,
                "value": {
                  "reading": 6
                }
              },
              {
                "file": "f03",
                "pos": 1,
                "value": {
                  "reading": 7
                }
              },
              {
                "file": "f04",
                "pos": 0,
                "value": {
                  "reading": 8
                }
              },
              {
                "file": "f04",
                "pos": 1,
                "value": {
                  "reading": 9
                }
              },
              {
                "file": "f05",
                "pos": 0,
                "value": {
                  "reading": 10
                }
              },
              {
                "file": "f05",
                "pos": 1,
                "value": {
                  "reading": 11
                }
              },
              {
                "file": "f06",
                "pos": 0,
                "value": {
                  "reading": 12
                }
              },
              {
                "file": "f06",
                "pos": 1,
                "value": {
                  "reading": 13
                }
              },
              {
                "file": "f07",
                "pos": 0,
                "value": {
                  "reading": 14
                }
              },
              {
                "file": "f07",
                "pos": 1,
                "value": {
                  "reading": 15
                }
              }
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 2,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ],
              [
                "f01",
                0
              ]
            ]
          }
        },
        {
          "op": "delete",
          "error": "BAD_POSITION",
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 0,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ],
              [
                "f01",
                0
              ]
            ]
          }
        }
      ]
    },
    "predicted": [
      {
        "logical": {
          "head": 2,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ]
          ]
        },
        "access": "方案B(M=64, h=0)：源快照校验仅点查 sid1 文件叶确认 f00 存在并核对载荷长度2，不枚举其余7个文件；BEGIN IMMEDIATE 后点查 head 根叶确认 f00 当前存在、dead_root 为空树，故非重复请求。墓碑树首叶插入编码键[f00,0]，COW 新增1个叶节点(含1条墓碑条目)，文件树0新节点，另写1行 snapshots(绑定原文件根与新墓碑根)与1行 head。预测持久元数据写入3条，与基线实测1/8/1/0中真正需要的1条墓碑同信息但不再复制8行 files。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ],
            [
              "f01",
              0
            ]
          ]
        },
        "access": "源与当前校验各只点查文件叶路径；墓碑树在现有dead叶上插入[f01,0]，复制该叶(2条目)及0层内部节点(h=0)，新增节点仍为1，文件树共享不写；另写 snapshots/head 各1。访问条目仅含目标叶的2条墓碑，不触碰其他文件与历史快照，同文件族多次稀疏删除只反复重写同一条键路径。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ],
            [
              "f01",
              0
            ]
          ]
        },
        "access": "校验路径同前；事务内 dead_contains 键路径点查命中已存在的[f00,0]，按 lake.py:35-36 语义直接返回 head=3，cow_apply 不执行，无节点、snapshots、head DML，预测持久写入0条，与基线实测全0一致；只读了有界的文件/墓碑叶路径。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ],
            [
              "f01",
              0
            ]
          ]
        },
        "access": "外层事务内完成点查后试写：新增1个含3条墓碑的dead叶(尝试)、1行 snapshots 与1行 head 更新；观察钩子随后抛异常，catalog.py:31-34 的 ROLLBACK 撤销该事务全部写入，head 仍为3、文件根与dead_root保持sid3旧值，尝试节点对后续不可达且编号可复用。持久化新增预测为0，与实测逻辑状态回退到第2步一致；回滚不读取或修改任何历史快照根。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ],
            [
              "f01",
              0
            ]
          ]
        },
        "access": "历史定位：按 sid=1 主键点查 snapshots 一次取得 file_root 与 dead_root(=NULL)，不沿 parent 重放、不扫描其他快照。深度遍历仅该根可达的1个文件叶(8条目)，dead 为空树短路；载荷逐文件读取得16活行。预测元数据读取为1行 snapshots+8条文件条目+0墓碑条目，与实测16行 read.result 一致，成本与 H 无关。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ],
            [
              "f01",
              0
            ]
          ]
        },
        "access": "sid=2 主键点查直达其根；遍历该快照自己的1个文件叶(8文件)，墓碑树点查访问含[f00,0]的1个叶(1条目)用于过滤，f00:0 被排除后返回15行。不读取 sid1/sid3 的任何节点，预测读取1+8+1条逻辑条目；历史输出与证据 actual 同形。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ],
            [
              "f01",
              0
            ]
          ]
        },
        "access": "sid=3 根指针直达；文件叶仍为8条目(单行删除不改文件集合)，dead 叶含[f00,0]、[f01,0]两条，仅遍历该根可达节点即得14行；无需重放前两代差分，预测读取1+8+2条，与实测 rows 长度14及逐行 value 完全一致。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ],
            [
              "f01",
              0
            ]
          ]
        },
        "access": "事务外先 resolve sid1 并点查文件叶确认 f00 存在，但载荷长度为2、pos=99 越界，按 lake.py:29-30 的错误顺序抛 BAD_POSITION，BEGIN IMMEDIATE 尚未发生，预测任何表 DML 为0，head 保持3；只访问单条文件键路径，不枚举清单。"
      }
    ]
  },
  "local": {
    "actual": {
      "initial": {
        "head": 1,
        "files": [
          "f00",
          "f01",
          "f02",
          "f03",
          "f04",
          "f05",
          "f06",
          "f07"
        ],
        "dead": []
      },
      "steps": [
        {
          "op": "delete",
          "snapshot": 2,
          "cost": {
            "read": {
              "snapshots": 2,
              "files": 16,
              "tombstones": 0,
              "head": 2
            },
            "write": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 1,
              "head": 1
            }
          },
          "logical": {
            "head": 2,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ]
            ]
          }
        },
        {
          "op": "prepare",
          "plan": {
            "base": 2,
            "inputs": [
              "f00"
            ],
            "outputs": [
              "rewrite-local-0",
              "rewrite-local-1"
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 1,
              "head": 1
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 2,
            "files": [
              "f00",
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07"
            ],
            "dead": [
              [
                "f00",
                0
              ]
            ]
          }
        },
        {
          "op": "publish",
          "snapshot": 3,
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 1,
              "head": 2
            },
            "write": {
              "snapshots": 1,
              "files": 9,
              "tombstones": 0,
              "head": 1
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07",
              "rewrite-local-0",
              "rewrite-local-1"
            ],
            "dead": []
          }
        },
        {
          "op": "prepare",
          "plan": {
            "base": 3,
            "inputs": [
              "rewrite-local-1"
            ],
            "outputs": []
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 9,
              "tombstones": 0,
              "head": 1
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 3,
            "files": [
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07",
              "rewrite-local-0",
              "rewrite-local-1"
            ],
            "dead": []
          }
        },
        {
          "op": "publish",
          "snapshot": 4,
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 9,
              "tombstones": 0,
              "head": 2
            },
            "write": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 0,
              "head": 1
            }
          },
          "logical": {
            "head": 4,
            "files": [
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07",
              "rewrite-local-0"
            ],
            "dead": []
          }
        },
        {
          "op": "read",
          "result": {
            "snapshot": 1,
            "rows": [
              {
                "file": "f00",
                "pos": 0,
                "value": {
                  "reading": 0
                }
              },
              {
                "file": "f00",
                "pos": 1,
                "value": {
                  "reading": 1
                }
              },
              {
                "file": "f01",
                "pos": 0,
                "value": {
                  "reading": 2
                }
              },
              {
                "file": "f01",
                "pos": 1,
                "value": {
                  "reading": 3
                }
              },
              {
                "file": "f02",
                "pos": 0,
                "value": {
                  "reading": 4
                }
              },
              {
                "file": "f02",
                "pos": 1,
                "value": {
                  "reading": 5
                }
              },
              {
                "file": "f03",
                "pos": 0,
                "value": {
                  "reading": 6
                }
              },
              {
                "file": "f03",
                "pos": 1,
                "value": {
                  "reading": 7
                }
              },
              {
                "file": "f04",
                "pos": 0,
                "value": {
                  "reading": 8
                }
              },
              {
                "file": "f04",
                "pos": 1,
                "value": {
                  "reading": 9
                }
              },
              {
                "file": "f05",
                "pos": 0,
                "value": {
                  "reading": 10
                }
              },
              {
                "file": "f05",
                "pos": 1,
                "value": {
                  "reading": 11
                }
              },
              {
                "file": "f06",
                "pos": 0,
                "value": {
                  "reading": 12
                }
              },
              {
                "file": "f06",
                "pos": 1,
                "value": {
                  "reading": 13
                }
              },
              {
                "file": "f07",
                "pos": 0,
                "value": {
                  "reading": 14
                }
              },
              {
                "file": "f07",
                "pos": 1,
                "value": {
                  "reading": 15
                }
              }
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 0,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 4,
            "files": [
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07",
              "rewrite-local-0"
            ],
            "dead": []
          }
        },
        {
          "op": "read",
          "result": {
            "snapshot": 2,
            "rows": [
              {
                "file": "f00",
                "pos": 1,
                "value": {
                  "reading": 1
                }
              },
              {
                "file": "f01",
                "pos": 0,
                "value": {
                  "reading": 2
                }
              },
              {
                "file": "f01",
                "pos": 1,
                "value": {
                  "reading": 3
                }
              },
              {
                "file": "f02",
                "pos": 0,
                "value": {
                  "reading": 4
                }
              },
              {
                "file": "f02",
                "pos": 1,
                "value": {
                  "reading": 5
                }
              },
              {
                "file": "f03",
                "pos": 0,
                "value": {
                  "reading": 6
                }
              },
              {
                "file": "f03",
                "pos": 1,
                "value": {
                  "reading": 7
                }
              },
              {
                "file": "f04",
                "pos": 0,
                "value": {
                  "reading": 8
                }
              },
              {
                "file": "f04",
                "pos": 1,
                "value": {
                  "reading": 9
                }
              },
              {
                "file": "f05",
                "pos": 0,
                "value": {
                  "reading": 10
                }
              },
              {
                "file": "f05",
                "pos": 1,
                "value": {
                  "reading": 11
                }
              },
              {
                "file": "f06",
                "pos": 0,
                "value": {
                  "reading": 12
                }
              },
              {
                "file": "f06",
                "pos": 1,
                "value": {
                  "reading": 13
                }
              },
              {
                "file": "f07",
                "pos": 0,
                "value": {
                  "reading": 14
                }
              },
              {
                "file": "f07",
                "pos": 1,
                "value": {
                  "reading": 15
                }
              }
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 1,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 4,
            "files": [
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07",
              "rewrite-local-0"
            ],
            "dead": []
          }
        },
        {
          "op": "read",
          "result": {
            "snapshot": 3,
            "rows": [
              {
                "file": "f01",
                "pos": 0,
                "value": {
                  "reading": 2
                }
              },
              {
                "file": "f01",
                "pos": 1,
                "value": {
                  "reading": 3
                }
              },
              {
                "file": "f02",
                "pos": 0,
                "value": {
                  "reading": 4
                }
              },
              {
                "file": "f02",
                "pos": 1,
                "value": {
                  "reading": 5
                }
              },
              {
                "file": "f03",
                "pos": 0,
                "value": {
                  "reading": 6
                }
              },
              {
                "file": "f03",
                "pos": 1,
                "value": {
                  "reading": 7
                }
              },
              {
                "file": "f04",
                "pos": 0,
                "value": {
                  "reading": 8
                }
              },
              {
                "file": "f04",
                "pos": 1,
                "value": {
                  "reading": 9
                }
              },
              {
                "file": "f05",
                "pos": 0,
                "value": {
                  "reading": 10
                }
              },
              {
                "file": "f05",
                "pos": 1,
                "value": {
                  "reading": 11
                }
              },
              {
                "file": "f06",
                "pos": 0,
                "value": {
                  "reading": 12
                }
              },
              {
                "file": "f06",
                "pos": 1,
                "value": {
                  "reading": 13
                }
              },
              {
                "file": "f07",
                "pos": 0,
                "value": {
                  "reading": 14
                }
              },
              {
                "file": "f07",
                "pos": 1,
                "value": {
                  "reading": 15
                }
              },
              {
                "file": "rewrite-local-0",
                "pos": 0,
                "value": {
                  "reading": 1
                }
              }
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 9,
              "tombstones": 0,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 4,
            "files": [
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07",
              "rewrite-local-0"
            ],
            "dead": []
          }
        },
        {
          "op": "read",
          "result": {
            "snapshot": 4,
            "rows": [
              {
                "file": "f01",
                "pos": 0,
                "value": {
                  "reading": 2
                }
              },
              {
                "file": "f01",
                "pos": 1,
                "value": {
                  "reading": 3
                }
              },
              {
                "file": "f02",
                "pos": 0,
                "value": {
                  "reading": 4
                }
              },
              {
                "file": "f02",
                "pos": 1,
                "value": {
                  "reading": 5
                }
              },
              {
                "file": "f03",
                "pos": 0,
                "value": {
                  "reading": 6
                }
              },
              {
                "file": "f03",
                "pos": 1,
                "value": {
                  "reading": 7
                }
              },
              {
                "file": "f04",
                "pos": 0,
                "value": {
                  "reading": 8
                }
              },
              {
                "file": "f04",
                "pos": 1,
                "value": {
                  "reading": 9
                }
              },
              {
                "file": "f05",
                "pos": 0,
                "value": {
                  "reading": 10
                }
              },
              {
                "file": "f05",
                "pos": 1,
                "value": {
                  "reading": 11
                }
              },
              {
                "file": "f06",
                "pos": 0,
                "value": {
                  "reading": 12
                }
              },
              {
                "file": "f06",
                "pos": 1,
                "value": {
                  "reading": 13
                }
              },
              {
                "file": "f07",
                "pos": 0,
                "value": {
                  "reading": 14
                }
              },
              {
                "file": "f07",
                "pos": 1,
                "value": {
                  "reading": 15
                }
              },
              {
                "file": "rewrite-local-0",
                "pos": 0,
                "value": {
                  "reading": 1
                }
              }
            ]
          },
          "cost": {
            "read": {
              "snapshots": 1,
              "files": 8,
              "tombstones": 0,
              "head": 0
            },
            "write": {
              "snapshots": 0,
              "files": 0,
              "tombstones": 0,
              "head": 0
            }
          },
          "logical": {
            "head": 4,
            "files": [
              "f01",
              "f02",
              "f03",
              "f04",
              "f05",
              "f06",
              "f07",
              "rewrite-local-0"
            ],
            "dead": []
          }
        }
      ]
    },
    "predicted": [
      {
        "logical": {
          "head": 2,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ]
          ]
        },
        "access": "点查 sid1 文件叶确认 f00 及载荷长度后开事务，当前根点查确认 f00 存活、墓碑树为空；COW 新建1个dead叶(1条目)，文件树不动，snapshots 与 head 各写1，预测持久写入3条；8个文件中仅 f00 的键路径被访问，写放大从基线8行 files 降为1个节点。"
      },
      {
        "logical": {
          "head": 2,
          "files": [
            "f00",
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07"
          ],
          "dead": [
            [
              "f00",
              0
            ]
          ]
        },
        "access": "无写事务：head 根点查后对唯一输入 f00 做文件成员点查(1叶路径)；expected/requested 全覆盖校验沿用集合比较，墓碑只做[f00,0]、[f01]所需的成员点查(命中1条)，过滤后输出 rewrite-local-0 含1行、空组产出零行 rewrite-local-1，载荷由 Objects 写、元数据0 DML。预测元数据写入0，与实测一致；不枚举其余7个文件的条目。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07",
            "rewrite-local-0",
            "rewrite-local-1"
          ],
          "dead": []
        },
        "access": "事务内先比 head==base 再点查 f00 成员，顺序保持 PUBLISH_CONFLICT/BAD_PLAN 语义；文件树对 erase(f00) 与 insert(两个输出) 施加于同一叶，重写1个节点(8→9条目，h=0)；墓碑树 erase_prefix(f00) 清空唯一条目，空叶不保留、新 dead_root=NULL；另写 snapshots/head 各1，预测持久写入3条(实测1/9/0/1的9条文件信息压缩进1个叶节点)。"
      },
      {
        "logical": {
          "head": 3,
          "files": [
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07",
            "rewrite-local-0",
            "rewrite-local-1"
          ],
          "dead": []
        },
        "access": "head 点查后对 rewrite-local-1 做一次文件叶点查即确认成员；layout 为空所以零墓碑点查，dead_root=NULL 全部短路；无输出载荷名、无元数据写入，预测0 DML，零行输入对应空 layout 的合法性与 lake.py:47-53 现状一致。"
      },
      {
        "logical": {
          "head": 4,
          "files": [
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07",
            "rewrite-local-0"
          ],
          "dead": []
        },
        "access": "事务内 base 校验通过；file_edits 仅含 erase(rewrite-local-1)，g=0 无插入，文件叶重写1个节点(9→8条目)；dead_edits 为空(NULL根)墓碑树0新节点；snapshots/head 各1，预测持久写入3条(实测1/8/0/1)。无输出发布仍正常递增快照号。"
      },
      {
        "logical": {
          "head": 4,
          "files": [
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07",
            "rewrite-local-0"
          ],
          "dead": []
        },
        "access": "sid=1 主键点查取根，dead_root=NULL；遍历其文件叶8条目并读8份载荷，无墓碑过滤，得16行。预测读取1 snapshots行+8文件条目，旧根在后续两次发布后均未被修改，输出可达。"
      },
      {
        "logical": {
          "head": 4,
          "files": [
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07",
            "rewrite-local-0"
          ],
          "dead": []
        },
        "access": "sid=2 根直达：文件叶8条目，dead 叶保留[f00,0]1条目并完成单点过滤，得15行；不重放 sid1→sid2 的删除差分，预测读取1+8+1条，与 actual read.result 逐行一致。"
      },
      {
        "logical": {
          "head": 4,
          "files": [
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07",
            "rewrite-local-0"
          ],
          "dead": []
        },
        "access": "sid=3 文件根叶含9个文件条目(含零行 rewrite-local-1，其载荷为空数组故贡献0行)，dead_root=NULL 无墓碑访问；遍历仅该根可达节点得15行(7个原文件14行+rewrite-local-0一行)，预测读取1 snapshots+9条目，f00 的旧墓碑已随输入移除但不影响本快照语义。"
      },
      {
        "logical": {
          "head": 4,
          "files": [
            "f01",
            "f02",
            "f03",
            "f04",
            "f05",
            "f06",
            "f07",
            "rewrite-local-0"
          ],
          "dead": []
        },
        "access": "sid=4 根叶含8条目(rewrite-local-1 已在无输出发布中移除)，无墓碑；一次 snapshots 点查加单叶遍历即得15行，预测读取1+8条，与基线 files 计数8及 read 行数15一致；全程不扫描 sid1..3 的历史节点。"
      }
    ]
  }
}
```

