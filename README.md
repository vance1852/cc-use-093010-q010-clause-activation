# 全球数字贸易合作运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理跨境数字合作中的资源流转、项目证据评估和统计资料质量。平台把运营节点、合作通道、资源申请、项目版本、分析决定、样本观测、权限和审计事件持久化到 SQLite，供秘书处、项目办公室、数据团队和审计人员协作使用。

## 目录

- src/trade_flow/：运营节点、合作通道、资源批次、额度申请、分配和情景分析；
- src/cooperation_assurance/：合作项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/metric_quality/：统计样本批次、指标观测、质量分析、账号权限和审批；
- src/clause_tracking/：条款提案基线、修订版本链、翻译对应、代表授权、保留意见、封存表决、前置条件、分方生效与后续行动；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m trade_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m cooperation_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m metric_quality.acceptance
    PYTHONPATH=src python3 -m clause_tracking.acceptance --workspace .

四条命令会在临时 SQLite 数据库中完成合作资源流转、项目证据评估、统计资料质量以及条款协商生效流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m trade_flow.api --database trade-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m cooperation_assurance.api --database cooperation-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m metric_quality.api --database metric-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m clause_tracking.api --database clause-tracking.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 条款协商与生效跟踪

`clause_tracking` 模块把条款文本的“被接受、条件满足、真正生效”区分为三个阶段：

1. **版本链**：每条条款有唯一提案基线，修订以父版本链接方式分叉并存，条款内相同内容自动去重；翻译文本带段落级对应关系。
2. **协商与封存**：参与方凭有效授权（可限定范围、可撤销）表达支持或保留意见，重复签署不增加支持数；秘书处按多数/三分之二/一致门槛封存表决，封存时的名单与统计固化为不可变事实，并发封存只能成功一次；互相冲突的修订不能同时封存合并。
3. **分方生效**：独立条款各自生效，非独立条款按一揽子组合生效；参与方接受文本后，前置条件（如国内批准、数据保护评估）全部办结才真正生效，条件持续保留责任人和期限。
4. **可重放依据**：条件完成、豁免、行动完成、撤销、终止等状态变化全部写入只追加事件日志并附 `basis_sha256`；同证据重复提交幂等返回，不同证据冲突报错。
5. **时点查询与逾期反查**：`GET /clauses/{id}/status?at=...` 仅依据事件日志重放任一时点各文本对各参与方的约束（accepted/in_force/terminated）；`GET /follow_ups/overdue` 从逾期行动反查条款、修订、封存票样、代表授权与前置条件证据链。
