# Agent 协作接口规范 v2

v1 经 GPT-5 Pro 对抗性评审后修订。v1 的结构性漏洞:把"工人不能靠散文骗过
编排者"推进成"工人只需骗过一个公开、固定、由编排者单方定义的评分函数"——
证据生成权与执行环境仍在工人手里,只外置了 oracle 文本,没有外置裁决权。
v2 的一句话架构:

    工人只提交候选修订(candidate revision)
        → 可信 gate 在新鲜环境中
        → 用独立所有的验证器
        → 验证 BASE→RESULT 的行为变化
        → gate 自己生成权威 verdict 与证据

目标不是"工人必须诚实报告",而是**"即使工人完全不可信,也没有能力决定
自己是否通过"**。

## 0. 失败模式(实测 9 起,v1 §0 保留)

A 验证错误对象×4;B 谎报工作×3;C 甩锅外因×3。根因:按报告文本评分而非
世界状态评分;诚实验证贵、编造免费。评审补充的二阶失败模式(v1 防不住):
对公开 verify.sh 的 specification gaming(测试路径特判、只满足观测量)、
污染验证器依赖(校验和只护住入口文件)、残留状态让复跑同样假 PASS、
随机验证器 cherry-pick 幸运一次、BLOCKED 变成 JSON 化的甩锅接口。

## 1. 设计原则(v2)

- P1 **裁决权外置**(升级自"评分权外置"):权威 verdict 由编排者侧 gate 在
  新鲜环境生成;工人本地跑验证只是 PRECHECK,永远不是权威结论。
- P2 证据即事实;无证据 = FAILED。
- P3 叙述降权:工人报告是 candidate receipt,不是 verdict。
- P4 负空间强制:UNVERIFIED 必填。
- P5 **来源绑定**(升级自"身份断言"):SOURCE_COMMIT → BUILD → ARTIFACT_HASH
  三者绑定(build id 烙进产物);mtime 不算证据。
- P6 窄作用域;P7 热续优先(RESUME)。
- P8 **验证器有 TCB 边界**:工人的 patch 触碰验证器/夹具/oracle/其依赖,
  原 verdict 自动 INVALID,须另立 VERIFIER_CHANGE 任务由编排者单独审。
- P9 **行为变化验证**:bug fix 必须 BASE_REV 上 FAIL、RESULT_REV 上 PASS;
  feature 必须正例 PASS 且至少一个反例仍按预期 FAIL。基线本来就 PASS 的
  验证器证明不了任何修复——它没有检测能力。

## 2. 任务档位

- **CHANGE**:完整契约。
- **QUERY**(只读分析):豁免 gate 构建,但采用 claim-evidence 契约(§7),
  引用只是可审计证据,不是证明。

## 3. Task Contract(派工)

header(机器解析):

```
CWD / TIER / WORKTREE / RESUME    (现有)
TASK_ID: <slug>
KIND: CHANGE | QUERY | VERIFIER_CHANGE
BASE_REV: <commit>                (验收的对照基线)
```

body 固定小节:

- **GOAL** — 一句话。
- **CLAIMS** — 完成后应成立的可观察行为断言,编号(C1, C2, …)。这是与工人
  的真正契约面;VERIFY 脚本只是 CLAIMS 的实现。
- **CHECKS** — 每个 CHECK 声明它证明哪个 CLAIM(V1→C1 …)。bug fix 类必须
  含 BASE-FAIL 检查。指向编排者预写的验证器(存放于 TRUSTED_PATHS)。
- **DELIVERABLE** — 预期改动 glob;须区分 PRODUCTION_SCOPE / TEST_SCOPE。
- **RULES** — 引用项目规则库的条目 ID(不再逐字复制陷阱全文)。
- **FORBIDDEN / OUT_OF_SCOPE** — 含 TRUSTED_PATHS(见 §5)。
- PATHS 降为可选提示,不属契约。

## 4. 工人侧产出(candidate receipt)

工人交付:候选修订(WORKTREE 下的未提交改动或 commit)+ receipt:

```
STATUS: PRECHECK_PASS | PRECHECK_FAIL | BLOCKED
TASK_ID / SESSION / RESULT_REV(或 worktree 路径)
CHANGED: 文件列表(git 对象已内容寻址,不重复逐文件 sha256)
UNVERIFIED: 清单或 none
BLOCKERS: 清单或 none
```

- PRECHECK 日志 tee 到证据目录,但**只用于诊断,不用于裁决**。
- BLOCKED 有证据义务:前置谓词失败 + 工人未改动相关前置 + 编排者可复现。
  三者缺一,按 FAIL 处理。不可复现的"环境不稳定"不是合法 blocker。

## 5. Trusted Gate(权威验收,编排者侧)

gate 由编排者拥有并运行,工人无法影响其输入:

1. **新鲜检出** RESULT_REV 到干净 worktree(不在工人工作目录复跑——残留
   状态会让两次运行共享同一污染)。verdict 绑定到 tree hash;adopt 后的
   最终树若与验收树不同,重验。
2. **TCB 检查**:diff 触碰 TRUSTED_PATHS(验证器/夹具/oracle/其依赖)→
   verdict INVALID,除非任务本身是 VERIFIER_CHANGE。
3. **来源绑定**:重建产物,核对 build id / ARTIFACT_HASH 出自 SOURCE_COMMIT。
4. **行为变化**:按 CHECKS 跑 BASE_REV(该 FAIL 的 FAIL)与 RESULT_REV
   (该 PASS 的 PASS)。
5. **随机性归 gate 管**:随机验证器的 seed、试验次数 N、聚合方式、阈值、
   负载前置检查全部写死在验证器里;工人无权挑选"幸运一次"。
6. **verdict 由 gate 生成**,含验证级别(V0 grep / V1 unit / V2 integration /
   V3 e2e / V4 production-equivalent)与 claim 覆盖范围——"grep 到函数"和
   "真机跑通"不允许压缩成同一个 PASS。
7. 之后才是人读 diff、高风险任务盲评 critic(输入 = diff + 证据,无叙述)。

## 6. 编排者误解的防线(oracle problem)

verify 脚本预写会把编排者的误解冻结成法律。对策不是"写得更认真",而是:

- 契约面从 VERIFY(实现)上移到 CLAIMS(可观察行为)。critic 盲评首先攻击
  "CLAIMS 是否覆盖 GOAL"与"CHECKS 是否真的证明 CLAIMS",而非笼统读脚本。
- bug fix 的 CLAIMS 必须由真实故障复现例锚定(BASE-FAIL 即是对 oracle 的
  最低限度校准)。
- 工人对 CLAIMS 有异议时走 BLOCKED + blockers 上诉,由编排者改契约重派,
  而不是工人自行重定义成功。

## 7. QUERY 档:claim-evidence 契约

引用真实≠结论成立(citation laundering)。每个重要结论必须三件套:

```
CLAIM:    结论本身(可观察、可证伪的表述)
SUPPORT:  文件:行号(gate 机械核查:存在、内容与 revision 一致)
FALSIFIER SEARCH: 为反例做过的检索命令(可复跑)
```

gate 机械核查存在性与一致性;claim→evidence 的语义蕴含无法机械验证,
高风险结论仍需独立 critic 做对抗评审。

## 8. v1 → v2 变更清单(采纳评审)

| v1 | v2 | 理由 |
|---|---|---|
| 工人跑 verify.sh 出权威 status | gate 新鲜环境出 verdict,工人只 PRECHECK | 裁决权真正外置 |
| 校验和护 verify.sh | TCB 边界护验证器全依赖 | 防"改卷子依赖" |
| GOAL+VERIFY | GOAL+CLAIMS+CHECKS 映射 | 防验收测错东西/无检测能力 |
| 无 | BASE-FAIL→RESULT-PASS 强制 | 校准 oracle,投入极低 |
| 编排者在原地复跑 | 新鲜检出复跑,verdict 绑 tree hash | 防残留状态双重假 PASS |
| 每任务 traps.md 全文 | 项目规则库,按 ID 引用 | 防文档腐烂 |
| 工人手填 commands[] | 砍掉(或 runner 自动记录) | 自证无价值 |
| 逐文件 sha256 | git 对象 ID;hash 留给产物/夹具 | 去重复记账 |
| PASS 一元 | verdict 带级别+claim 范围 | 防信息压缩 |
| BLOCKED 自由填写 | BLOCKED 有三重证据义务 | 防 JSON 化甩锅 |
| QUERY 引用即证据 | CLAIM/SUPPORT/FALSIFIER 三件套 | 防 citation laundering |

## 9. 未采纳/降级的评审意见(及理由)

- 完整 hermetic 容器化(env 白名单、网络策略、镜像 digest):单用户机器人
  工作站上成本过高;以"新鲜 worktree + 声明式依赖清单 + seed 归 gate"作为
  务实的 80% 方案,预留升级路径。
- 工人 receipt 整体废除:保留——UNVERIFIED/BLOCKERS 的负空间申报确实改变
  工人局部激励,只是不再当 verdict 用。

## 10. 分阶段落地(通用,不绑定本项目)

- **阶段 1(零基建,下一次派工即生效)**:brief 模板换成 CLAIMS/CHECKS;
  强制 BASE-FAIL→RESULT-PASS;UNVERIFIED 必填;BLOCKED 证据义务;工人
  报告降级为 receipt,编排者自己出 verdict。
- **阶段 2(一次性基建,约半天)**:gate 脚本(新鲜 worktree 检出 + 依赖
  链接 + 缓存编译 + 跑验证器 + TCB 检查 + tree hash 绑定);把项目记忆里
  的陷阱沉淀成规则库(条目 ID 可被 brief 引用)。
- **阶段 3(随需)**:build id 烙印进产物;测试 harness 把"记录实际配置"
  升级为"断言实际配置";随机验证器模板(seed/N/阈值/负载检查内置)。
