# 断联补账（recon）

热量表偶尔断联或读数跳变，断联那段热量在月末结算时丢失、说不清少多少。本模块按
**前后读数、当时的流量和温差**把缺失段补出来，给出补账结果、差异说明和可接上的
回绕前后读数；补账结果同时供控制台展示和审计落盘。

## 定死的口径（不可在运行期临时改）

1. **补算方法按表型定**（由表型配置 CSV 的 `kind` 指定）：
   - `rate`（上报流量/供回温分量）：按 `功率 = k_factor × 流量 × (供温−回温)`
     反算瞬时功率，断联段做梯形积分并逐槽分配；入账量取寄存器权威读数差，反算值
     只负责对账。对账相对残差超过 2%（`closure_tolerance`）整段标缺。
   - `total`（只有累计热量寄存器）：优先用**回绕解绕后的两端读数差**线性分配到
     缺失槽；只有当读数冻住（读数差为 0 但分量显示在用热）**且**缺口在短档内时，
     才用断联两侧相邻时段的每槽增量插值，并要求两侧都有合格邻居。

2. **按阈值分档，不做"回绕前一律补满"**（`Policy.fill_rule = "threshold_tiered"`）：
   - 短档：缺失 ≤ **2 个采集槽**，允许估算（反算 / 读数差分配 / 冻住插值）；
   - 中档：>2 槽且段时长 ≤ **24 小时**，只接受有物理依据的补法（rate 反算、
     total 读数差分配），total 表不允许邻居插值；
   - 长档：>24 小时，**一律标缺**。即使 total 表两端读数差能量化，也只在差异
     说明里挂账披露（`unbooked_register_kwh`），不计入入账热量。

3. **缺条件宁可标缺，不许硬凑**。标缺原因码：
   `MISSING_LONG`（长档）、`MISSING_CONDITION`（缺流量/温差或中档冻住）、
   `MISSING_NEIGHBOR`（插值缺合格邻居）、`RATE_CLOSURE_MISMATCH`（反算与读数差
   对账超容差）、`AMBIGUOUS_ROLLOVER`（回绕次数无法唯一确定）、`BAD_SAMPLE`
   （读数跳变越限的坏点）、`NO_ANCHOR`（月窗边缘无读数锚点）。

4. **历史不可变，只能追加**：
   - `raw-*.jsonl`：原始采样归档，永不回写；
   - `ledger-*.jsonl`：每次补账一条不可变结果，月度结算取该月 `run_no` 最大的
     一条（最新账），历史批次可回放；
   - `audit-*.jsonl`：哈希链审计（`prev_sha256` 链 + 每条自身 `sha256`），
     篡改、删除、乱序都会在 `verify` 中断链。

5. **补账、总账、审计同源一致**：所有展示（控制台文本、HTTP JSON、审计）都从同一个
   `ReconciliationRun` 派生；引擎在落账前强制核对总账恒等式，不闭合直接抛
   `ConsistencyError`，不带半份不一致的账落盘。

## 总账恒等式（差异说明口径）

```
寄存器读数差合计 registered = 直接实读 observed
                 + 断联补入 filled（其中读数差类补入占读数差恒等式）
                 + 标缺段读数差披露 unbooked_register（只披露、不入账）
入账合计 booked = observed + filled
表计修正 correction：total 读数冻住时靠旁证补入的量（读数差为 0，单独列示）
反算残差 residual：rate 反算原始量与读数差之差（只披露，不改入账量）
```

回绕前后的读数通过 `anchors`（`raw_kwh` 表头读数 / `unwrapped_kwh` 连续读数）
和每段的 `anchor_before_kwh` / `anchor_after_kwh` / `wraps` 接得上。

## 输入格式

原始采样 CSV（ISO8601 或 epoch 秒；分量列可空）：

```
ts,meter,energy_kwh,flow_t_h,supply_c,return_c
2026-09-01T00:00:00Z,R-1001,10.5,30,80.0,77.1
```

表型配置 CSV：

```
meter,kind,wrap_kwh,period_s,k_factor,max_power_kw
R-1001,rate,1000000,3600,1.163,40
T-2001,total,1000,3600,1.163,60
```

`max_power_kw` 是回绕解绕的限幅依据：枚举 `raw + n×wrap` 候选时，超过
`最大功率 × 槽跨度` 的候选剔除；唯一候选才采纳，0 个 = 坏点隔离，多个 = 回绕
多解断链。缺省（空）时按"累计热量不减"取最小非负增量。

## 使用

```
make recon-demo                                   # 生成演示 CSV（覆盖全部情形）
make recon-run                                    # 执行补账，控制台打印补账+差异+总账
python -m heatnet.recon.cli run ... --json        # 同源 JSON
python -m heatnet.recon.cli latest --month 2026-09
python -m heatnet.recon.cli list
python -m heatnet.recon.cli audit --month 2026-09
python -m heatnet.recon.cli verify                # 哈希链与总账一致性校验
```

存在标缺段属于**部分失败**：命令照常退出、结果以 `PARTIAL` 和 warning 审计明示，
结算前需人工确认；审计链断裂或总账不闭合属于系统错误，退出码 2。

HTTP 接口：`POST /api/recon/run`（month/profiles/samples）、
`POST /api/recon/demo`、`GET /api/recon/latest?month=`、`GET /api/recon/runs`、
`GET /api/recon/audit`、`GET /api/recon/verify`。
