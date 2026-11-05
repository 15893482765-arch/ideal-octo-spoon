# Q171/Q172 备份恢复演练记录

- 演练时间（UTC）：2026-10-15 07:55
- 执行人：T29 自动化（`infra/drill_restore.sh`）
- 源库：PostgreSQL 16.4 + PostGIS（容器 `qinshuang-postgres`，逻辑库 `qinshuang`，71 张表）
- 恢复目标：独立 DR 库 `qinshuang_dr`（演练前 DROP + CREATE，确保干净）
- 备份工具：`pg_dump --no-owner --no-privileges | gzip`（明文 SQL，可解压校验）

## 结果

| 项 | 结果 |
|---|---|
| 备份文件 | `/backups/backup-20261015T075538Z.sql.gz` |
| 归档大小 | 19735 bytes |
| sha256 | `75171cf8f903e8eec514d2ee39e01132247f0147fcb771e597161ce7fc25fac8` |
| 恢复墙钟耗时（RTO 实测） | **1 秒**（目标 ≤4h） |
| RPO 校验（最新标记数据在 DR 库） | 通过（`t29_drill_marker.note = q171-backup-restore-drill`） |
| DR 库表数量 | 71（与源一致） |
| 结论 | **DRILL_OK：RPO≤24h=true，RTO≤4h=true** |

## 安全要点

- 数据库口令仅经 `PGPASSWORD` 环境变量注入 `pg_dump/psql`，**不进 argv**（进程列表不可见），
  对应单测 `test_q171_dump_command_never_puts_password_in_argv`。
- 恢复前对归档做 gzip 完整性 + sha256 校验，损坏即 `BACKUP_CORRUPT`，不静默成功
  （单测 `test_q171_corrupt_archive_blocks_restore`）。
- 备份失败不留半成品文件、不写清单（单测 `test_q172_failed_dump_raises_and_leaves_no_file`）。
- 保留窗口 30 天（`BACKUP_RETENTION_DAYS=30`），超期备份在每日备份后清理
  （单测 `test_q171_retention_prunes_backups_older_than_30_days`）。

## 生产调度（运维落地，非本仓代码外）

- 每日 02:00（业务低峰）CronJob 执行 `python scripts/backup.py backup`；
- 每周日执行一次 `python scripts/backup.py drill`，恢复到隔离 DR 库并校验 RPO/RTO；
- 备份归档需同步到对象存储异地桶（开启版本 + Object Lock），MinIO 仅作本地落地层；
- 绩效/工资数据长期保留：工资单 `payroll_*` 表不参与 30 天清理，按合规要求（建议 ≥7 年）
  在对象存储冷归档长期保留，应用层已保证已审核事实不物理删除。

> 注：本次演练数据量为空库结构 + 1 行标记，RTO=1s 仅证明链路与正确性；
> 满量数据（约 5 万门店、多年销量）的 RTO 需在预发环境以等大快照定期实测，
> 列入上线前验收项，不得用本次空库数字对外承诺全量 RTO。
