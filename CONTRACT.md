# 快照元数据局部更新：讨论工作区

## 当前仓库
lake.py 是业务入口，catalog.py 保存快照及 head，objects.py 保存不可变行文件；这三份源码保持原样。
checks.py 是正常业务回归；replay.py、samples.py、examples/move.json 保留四种重写时序，观察现有冲突语义。
observe.py 只包装 SQLite 调用并重放有限样例，不提供目标存储结构。
旧题的 review.py 不随本题提供，也不适用于本题。

## 业务接口
Python 标准库及 SQLite，无第三方依赖。默认内存适配使每次重放独立。
磁盘适配是 Lake(Catalog("work/catalog.db"), Objects("work/objects"))，调用者先创建 work 目录。
文件名仅含字母、数字、减号、下划线，永久唯一、不复用；同名对象只允许相同规范内容。行是 JSON 对象，可同值但不是同一物理位置。

- Lake.seed(files)：files 是文件名到行数组的映射，空库创建快照1；快照0为空。已有数据报 ALREADY_SEEDED。
- Lake.read(snapshot=None)：默认固定一次 head，返回 {"snapshot":整数,"rows":[{"file":文件名,"pos":零起行号,"value":原对象}]}，按文件名和行号排序。先排除墓碑，历史快照永久保留。
- Lake.delete(snapshot,fid,pos)：源快照不存在报 NO_SNAPSHOT；文件不在源快照、非整数或越界位置报 BAD_POSITION；当前已没有此文件报 STALE_LAYOUT。当前已有该墓碑则返回 head，不新增快照；否则原子发布新快照。错误检查顺序沿源码。
- Lake.prepare(job,inputs,layout)：inputs 必须是非空、无重复且属于当前快照的文件列表，否则 BAD_INPUTS。layout 为输出组数组，每组含 [输入文件,原行号]；全部输入物理位置必须恰好出现一次，包括已删位置，否则 BAD_LAYOUT。可以有空组，零行输入可以对应空 layout。写出 rewrite-JOB-组号，跳过基准快照墓碑，返回 {"base":快照号,"inputs":[文件名],"outputs":[文件名]}。调用者使用新 job，不篡改计划。
- Lake.publish(plan)：仅 head 等于 base 时替换输入，否则 PUBLISH_CONFLICT；输入不合法报 BAD_PLAN。输出文件包括零行文件，移除输入时同时移除其当前墓碑。创建新快照，旧快照不变。
- Lake.rewrite 是同一写事务内的 prepare/publish 封装；其锁范围沿用现状，不在本题优化。
Catalog.write 使用 BEGIN IMMEDIATE，嵌套调用加入外层事务；异常逃出最外层时回滚。元数据与 head 原子生效，Objects 载荷不在该事务中。外层回滚可以留下不可达对象，不改变历史读取。

## 可重跑观察
从仓库根目录运行：
- python3 checks.py：打印 BASELINE_OK 并核对现有业务。
- python3 replay.py all：每行输出一个 case 和 trace；normal、before、between、after 的当前行数分别为4、3、3、4，历史均为4。between 保留 PUBLISH_CONFLICT，after 保留 STALE_LAYOUT。
- python3 observe.py verify：重跑正常回归并核对本题样例的实际元数据计数、历史读取和回滚。
- python3 observe.py demo：每行输出 {"case":名称,"actual":观察结果}。
- python3 observe.py run PACK.json：读取与 examples/metadata.json 相同格式的其他有限样例。

样例包顶层为名称到样例的映射，每个样例有 files 与 operations。
delete 含 snapshot、file、pos；snapshot 为整数或 "initial"。
prepare 含 job、inputs、layout；publish 使用最近一次准备的计划。
read 的 snapshot 可省略；rollback_delete 与 delete 字段相同，但在外层事务内执行后主动抛异常，验证整次元数据回滚；这只是观察钩子。

actual.initial 为初始逻辑状态；actual.steps 按操作顺序保存 op、返回的 snapshot/plan/result 或 error、cost、logical。
logical 固定为 {"head":整数,"files":[有序文件名],"dead":[有序的[文件名,行号]]}，表示操作后当前快照。
read.result 则保存指定快照的完整行值，不能用 logical 的当前状态冒充历史读取。
cost.read 按 snapshots/files/tombstones/head 统计 SQL 游标实际交给调用者的行数；cost.write 统计 DML 实际影响的行数。
包括验证源快照和读取当前快照，prepare 与 publish 分别计量；排除初始化及观察器附加的 logical 读取。
回滚操作的 write 仍记录已执行但后来回滚的写入，不能当成持久新增。
计数不是数据库页 I/O、SQL 虚拟机访问数或耗时，不据此声称隐藏扫描不存在。

公开样例：
sparse 有8个文件、各2行。两次不同位置删除后，发布的 files 行各为8，tombstones 行为1和2；重复删除不提交；回滚探针执行过8条文件写入及3条墓碑写入，但 head 仍为3。读取快照1、2、3分别返回16、15、14行；最后的越界删除报 BAD_POSITION。
local 先删 f00:0，再把 f00 重写为一个含剩余行的文件和一个零行文件；发布写9条 files，当前不再有 f00 墓碑。随后以空 layout 重写零行文件，输出列表为空，发布写8条 files。快照1至4行数分别为16、15、15、15，旧快照的文件与墓碑不变。

## 本轮设计边界
保持 Lake 外部接口、结果、错误优先级、快照编号及提交条件。Catalog 内部端口可以提出替换。
单位置删除不得枚举或复制整份文件集合、全体墓碑；重写准备及发布只需访问输入、输出相关元数据和必要索引路径。
允许访问及复制必要索引路径上的有界节点，节点内附带条目必须计入成本，不能把整份清单改名为一个索引节点。
历史读取可以遍历目标快照实际包含的文件及墓碑、必要索引路径，不能逐代重放差分，或扫描全部历史版本后按 sid 筛选。
seed 可以按全部输入建立索引。不要求随机读取单行、修改行文件格式或减少原本必须读取的输入载荷；只设计元数据访问。
给出随文件数 F、当前墓碑数 D、历史快照数 H、本次涉及文件数 k 与墓碑数 d 变化的读写和额外存储上界；自定义索引节点容量或其他参数须说明含义。
分析应覆盖大量无关文件或历史、同文件多次稀疏删除、重复删除、移除带墓碑输入、空文件、无输出发布和外层回滚。保留原 staged 冲突语义；无需使旧位置删除穿过重写。
无需实现目标结构、迁移旧库、回收载荷、分支、多维护者协调或新恢复框架。

## 文档交付与证据核对
只交付 DESIGN.md：定位 Lake 到 Catalog 及载荷的调用链，引用 文件名:行号；比较至少两种存储策略，选定一种满足边界的方案，说明结构、索引、替换端口、提交伪代码、历史一致性及访问成本。
伪代码须具体到查询条件、修改哪些记录、根或快照标识如何绑定及事务开始/提交/回滚；不要求实现可运行的新引擎。
分别标注基线实测与设计预测，说明必要状态及为何无需重放全部历史，不能以文档结构检查通过代替正确性论证。

DESIGN.md 中放一个以 <!-- metadata-evidence --> 开头的 json 代码块，顶层恰有 sparse、local：
每项的 actual 是 observe.run 对该样例生成的完整结果；
predicted 是与 actual.steps 等长的数组，每项含 logical、access：
logical 是方案预测的同形当前逻辑状态；
access 是非空文字，逐步解释方案访问或复制哪些元数据、成本及原因；read 步还须解释指定历史快照如何定位，回滚步须解释未发布状态的处置。
保留实际 read.result；文档正文应核对这些历史输出在推荐结构中的可达性，不把 metadata 证据写成目标结构代码。
本题不预设索引算法，计数预测由具体设计及人工审查核对。

交付后将 .pointwise-runtime.json 的 verify 改为 ["python3","design_check.py"]，demo 保持 ["python3","observe.py","demo"]。
python3 design_check.py 重跑回归及观察器，核对文档实际轨迹、预测逻辑状态和有效源码行号；访问边界、成本与事务伪代码仍须人工审查。
保留生产源码、原回归、原样例与观察器，不修改断言来配合文档。
