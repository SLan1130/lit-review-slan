# 工作流详解

## 完整流程图

PDF目录 → pdf_metadata_v3.json → load_local_papers() → papers + summaries

Step1聚类(pro模型) → themes列表
Step2分配(flash模型) → assignments字典
生成结构(JSON) → structure对象

并行写作(Writer) → written_sections
并行自审(Reviewer) → reviewed_sections
组装输出 → output.md

## Step1: 主题聚类

### 分批提取子主题
- 每批15篇文献
- pro模型提取2-3个核心子主题
- 格式：子主题1: xxx

### 合并子主题
- 将所有子主题合并
- pro模型生成4-5个主主题
- 主题命名≤15字

## Step2: 文献分配

### 批量分配
- 每批10篇文献
- flash模型输出JSON
- 动态上限：max(8, n//k+2)

### 均衡调整
- 超限文献重分配到最少主题
- 未分配文献均衡归入各节

## 并行写作(Writer)

- 2线程并行
- pro模型，max_tokens=8192
- 每节500-800字学术综述
- 失败自动降级到flash

## 并行自审(Reviewer)

检测项：
- 截断检测：段落末尾非完整句子
- 长度检测：段落<150字

问题段落重写：pro模型，400-600字

## 缓存机制

缓存目录：.cache/
- {hash}.json: API调用缓存
- last_papers.json: 上次文献数量

缓存策略：
- 基于prompt哈希
- 增量模式：文献数量未变跳过处理
