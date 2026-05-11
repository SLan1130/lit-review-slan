---
name: lit-review-slan
description: 多智能体文献综述系统。基于本地PDF元数据，自动完成文献聚类、分配、综述生成与自审的全流程。触发场景：(1)用户请求生成文献综述；(2)用户提到整理XX主题的文献；(3)用户需要基于PDF生成研究综述；(4)执行literature_review_final.py脚本。核心能力：LLM辅助的主题聚类、摘要分配、并行写作、自审修订、分布式调度（熔断器+重试）。
---

# Lit Review Slan - 多智能体文献综述系统

## 概述

基于DeepSeek API的多智能体文献综述系统，将文献综述工作流自动化。

核心流程：加载PDF元数据 → Step1主题聚类 → Step2摘要分配 → 并行写作 → 自审修订 → 输出Markdown

## 工作流程

`
StructurePlanner: Step1聚类(pro) → Step2分配(flash) → 生成结构(JSON)
Writer: 并行写作(2线程) → pro模型(8192 tokens) → 输出段落(500-800字)
Reviewer: 并行自审(2线程) → 截断/长度检测 → 重写修订(pro模型)
`

## 快速开始

`ash
export DEEPSEEK_API_KEY='your-api-key'
python literature_review_final.py --topic 研究主题 --output 综述.md
`

## 核心组件

### 1. API调用层 (call_ds)

- 熔断器机制：连续5次失败触发60s强制等待
- Jittered退避：delay = base × 2^n × random(0.5, 1.5)
- 调用前延迟：0.3-1.5s随机错峰
- 自动降级：pro失败自动切换flash模型
- 结果缓存：基于prompt哈希缓存

### 2. StructurePlanner

**Step1聚类（pro模型）**：
- 分批处理（每批15篇）避免prompt过长
- Phase1：提取子主题（每批2-3个）
- Phase2：合并为4-5个主主题
- 主题命名≤15字，递进/并列关系

**Step2分配（flash模型）**：
- 基于摘要内容分配文献
- 动态上限：max(8, n//k+2)
- 未分配文献均衡归入各节

### 3. Writer（pro模型）

- 并行写作（2线程）
- max_tokens=8192
- 每节500-800字学术综述
- 自动降级到flash

### 4. Reviewer（pro模型）

- 并行自审（2线程）
- 检测项：截断末尾、段落过短
- 问题段落重写

## 分布式调度机制

### CircuitBreaker熔断器

`python
_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60,
)
`

状态转换：
- CLOSED → (连续5次失败) → OPEN
- OPEN → (60秒后) → CLOSED
- OPEN → (1次成功) → CLOSED

## 详细文档

- 工作流详解：references/workflow.md
- 使用文档：references/usage.md
- 核心脚本：scripts/literature_review_final.py
- PDF提取：scripts/extract_pdf_v3.py
