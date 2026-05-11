# lit-review-slan 多智能体文献综述系统

基于DeepSeek API的多智能体文献综述自动化系统，通过StructurePlanner、Writer、Reviewer三个Agent协作，将文献综述工作流自动化。

## 目录

- [工作流程](#工作流程)
- [核心组件](#核心组件)
- [安装与配置](#安装与配置)
- [使用方式](#使用方式)
- [数据格式](#数据格式)
- [故障排除](#故障排除)
- [架构详解](#架构详解)

---

## 工作流程

`
┌─────────────────────────────────────────────────────────────────┐
│                        StructurePlanner                           │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  Step1聚类   │ →  │  Step2分配   │ →  │  生成结构    │      │
│  │  (pro模型)   │    │  (flash模型) │    │  (JSON)      │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                           Writer                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  并行写作    │ →  │  pro模型     │ →  │  输出段落    │      │
│  │  (2线程)     │    │  (8192 tokens)│   │  (500-800字)│      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                          Reviewer                                │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  并行自审    │ →  │  截断检测    │ →  │  重写修订    │      │
│  │  (2线程)     │    │  长度检测    │    │  (pro模型)   │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
└─────────────────────────────────────────────────────────────────┘
`

---

## 核心组件

### 1. StructurePlanner（结构规划Agent）

**职责**：理解文献主题，生成综述结构

**Step1 - 主题聚类（pro模型）**

`
输入: 41篇文献的标题和摘要
  ↓
分批处理（每批15篇）
  ↓
Phase1: 提取子主题
  - 每批调用pro模型提取2-3个核心子主题
  - 格式: 子主题1: xxx
  ↓
Phase2: 合并子主题
  - 将所有子主题合并
  - 调用pro模型生成4-5个主主题
  - 约束: 命名≤15字，递进/并列关系
  ↓
输出: ["战略逻辑与体系建构", "政策实施与制度演化", ...]
`

**Step2 - 文献分配（flash模型）**

`
输入: 主题列表 + 文献摘要
  ↓
批量分配（每批10篇）
  ↓
调用flash模型输出JSON
  - 每篇分配到最相关主题
  - 动态上限: max(8, n//k+2)
  ↓
均衡调整
  - 超限文献重分配到最少主题
  - 未分配文献均衡归入各节
  ↓
输出: 
{
  "sections": [
    {"id": "3.1", "title": "战略逻辑与体系建构", "papers": [...], "rationale": "..."},
    ...
  ]
}
`

### 2. Writer（写作Agent）

**职责**：基于分配的文献撰写综述段落

**工作流程**：

`
输入: 单节信息（主题 + 分配文献）
  ↓
构建prompt
  - 节主题说明
  - 文献标题+摘要列表
  - 要求: 500-800字学术综述
  ↓
调用pro模型
  - max_tokens: 8192
  - 失败自动降级到flash
  ↓
返回: {"text": "...", "source": "pro/flash", "papers": [...]}
`

**并行机制**：

`python
with ThreadPoolExecutor(max_workers=2) as executor:
    futures = {executor.submit(write_review_section, sec, papers, summaries): sec["id"]
               for sec in sections}
`

- 2线程并行处理多个节
- 降低API并发压力

### 3. Reviewer（审核Agent）

**职责**：检测并修复问题段落

**检测项**：

| 检测类型 | 触发条件 | 说明 |
|----------|----------|------|
| 截断检测 | 段落末尾非完整句子 | 可能是max_tokens不足导致 |
| 长度检测 | 段落<150字 | 内容不完整或生成失败 |

**工作流程**：

`
扫描所有节
  ↓
检测问题
  ↓
问题段落重写
  - 调用pro模型
  - 要求: 400-600字完整段落
  ↓
成功: 替换原文本
失败: 保留原文本
`

---

## 安装与配置

### 环境要求

- Python 3.8+
- DeepSeek API Key

### 安装步骤

**1. 克隆仓库**

`ash
git clone https://github.com/SLan1130/lit-review-slan.git
cd lit-review-slan
`

**2. 安装依赖**

`ash
pip install requests
`

**3. 配置API Key**

`ash
# Linux/Mac
export DEEPSEEK_API_KEY='sk-your-api-key'

# Windows PowerShell
='sk-your-api-key'
`

**4. 准备PDF元数据**

在PDF目录放置 pdf_metadata_v3.json：

`json
[
  {
    "title": "论文标题",
    "authors": ["作者1", "作者2"],
    "year": "2024",
    "journal": "期刊名",
    "summary_cn": "中文摘要（用于分配文献）",
    "filename": "paper.pdf"
  }
]
`

---

## 使用方式

### 命令行运行

`ash
python literature_review_final.py --topic "中国制造2025产业政策研究" --output "综述.md"
`

### 参数说明

| 参数 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| --topic | 是 | 研究主题 | - |
| --output | 否 | 输出文件路径 | literature_review.md |
| --incremental | 否 | 增量模式 | False |
| --pdf-dir | 否 | PDF元数据目录 | WPS云盘路径 |

### Python调用

`python
from literature_review_final import (
    load_local_papers,
    structure_and_plan,
    write_review_parallel,
    review_parallel,
    generate_summary_and_conclusion
)

# 加载数据
papers, summaries = load_local_papers(pdf_dir)

# 生成结构
structure = structure_and_plan(papers, summaries)

# 并行写作
written = write_review_parallel(papers, summaries, structure)

# 并行自审
reviewed = review_parallel(written)

# 生成总结
summary = generate_summary_and_conclusion(reviewed, papers)
`

---

## 数据格式

### 输入格式（pdf_metadata_v3.json）

`json
[
  {
    "title": "论文标题",
    "authors": ["作者1", "作者2"],
    "year": "2024",
    "journal": "期刊名",
    "summary_cn": "中文摘要，用于LLM理解文献主题并分配到合适的综述节",
    "filename": "paper.pdf"
  }
]
`

### 输出格式（Markdown）

`markdown
# [研究主题] 文献综述

## 3.1 战略逻辑与体系建构
基于摘要内容分配的文献综述段落，500-800字。

本节围绕...展开论述。Chen等(2020)指出...。同时，Wang(2021)认为...

## 3.2 政策实施与制度演化
本节聚焦...。研究发现...（作者, 年份）...

...
`

---

## 故障排除

### API空返回

**现象**：API返回HTTP 200但content为空

**原因**：DeepSeek服务器负载限制

**解决方案**：
1. 等待5-10分钟后重试
2. 避开晚间高峰期（18:00-23:00）
3. 使用增量模式：--incremental

### 响应超时

**现象**：API调用超时

**解决方案**：
1. 检查网络连接
2. 启用增量模式跳过已处理部分
3. 等待API恢复

### 主题分配不均

**现象**：某些节文献过多/过少

**原因**：flash模型在负载高时不稳定

**解决方案**：
1. 降低熔断器阈值
2. 手动调整max_per_section参数

### 代码错误

**查看日志**：运行时会输出详细日志

`
[17:13:36] [INFO] [Loader] 加载了 41 篇本地PDF文献
[17:13:37] [INFO] [API] 调用 deepseek-v4-pro
[17:13:51] [INFO] [API] 成功 (14.5s, 98字符)
`

---

## 架构详解

### API调用层（call_ds）

`
┌─────────────────────────────────────────┐
│              call_ds()                   │
├─────────────────────────────────────────┤
│ 1. 熔断器检查                            │
│    - 连续5次失败 → 强制等待60秒           │
│ 2. 调用前延迟                            │
│    - 0.3-1.5秒随机错峰                   │
│ 3. 请求发送                              │
│    - max_tokens: 4000 (默认)             │
│    - timeout: 300秒                      │
│ 4. 重试逻辑                              │
│    - 最多4次重试                         │
│    - Jittered退避: 1-3s, 2-6s, 4-12s    │
│ 5. 降级机制                              │
│    - pro失败 → 自动切换flash             │
│ 6. 缓存                                  │
│    - 基于prompt哈希缓存结果              │
└─────────────────────────────────────────┘
`

### CircuitBreaker熔断器

`python
_circuit_breaker = CircuitBreaker(
    failure_threshold=5,      # 连续5次失败触发
    recovery_timeout=60,      # 强制等待60秒
)
`

**状态转换图**：

`
┌─────────┐  连续5次失败   ┌────────┐
│ CLOSED │ ─────────────→ │  OPEN  │
└─────────┘                └────────┘
     ↑                          │
     │                          │ 60秒后
     │ 1次成功                   ↓
     └──────────────────── ┌────────┐
                           │CLOSING │
                           └────────┘
`

### 重试策略

| 尝试 | Jittered退避范围 | 说明 |
|------|------------------|------|
| 1 | 0.5s - 1.5s | 首次失败 |
| 2 | 1s - 3s | 短暂等待 |
| 3 | 2s - 6s | 中等等待 |
| 4 | 4s - 12s | 最后尝试 |

### 缓存机制

`
缓存目录: .cache/
├── {hash}.json      # API调用缓存
└── last_papers.json # 上次文献数量

增量模式: 文献数量未变时跳过处理
`

---

## 文件结构

`
lit-review-slan/
├── SKILL.md                    # SOLO Skill定义
├── README.md                   # 本文档
├── lit-review-slan.skill       # 打包文件（可分发）
└── scripts/
    ├── literature_review_final.py  # 核心workflow (v18)
    └── extract_pdf_v3.py          # PDF元数据提取
`

---

## 版本历史

| 版本 | 更新内容 |
|------|----------|
| v18 | 添加CircuitBreaker熔断器，优化分布式调度 |
| v17 | 优化主题命名约束，修复分配均衡问题 |
| v16 | 2线程并行，max_tokens=8192 |
| v15 | 初始版本，StructurePlanner + Writer + Reviewer |

---

## License

MIT License
