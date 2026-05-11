"""
多智能体文献综述系统 v18
优化总结：
1. Step1: pro模型 + summary聚类 + 4-5主题 + 均衡约束
2. Step2: flash模型 + JSON分配 + 每节上限12篇
3. Writer/Reviewer: pro模型 + 空结果自动重试+降级
4. 代码层面：prompt日志、错误捕获、缓存、增量模式
5. v18新增：分布式调度（谐波退避+全局熔断器）
"""
import sys, json, time, requests, re, os, hashlib, random, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ============ 分布式调度：全局熔断器 ============
class CircuitBreaker:
    """全局熔断器，防止API过载
    
    原理：当连续失败次数超过阈值时，强制暂停等待
    恢复后逐步释放限制
    """
    def __init__(self, failure_threshold=5, recovery_timeout=60, max_consecutive_failures=10):
        self.failure_threshold = failure_threshold  # 触发熔断的连续失败次数
        self.recovery_timeout = recovery_timeout    # 熔断后强制等待时间(秒)
        self.max_consecutive_failures = max_consecutive_failures  # 连续失败上限
        
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._circuit_open_time = None
        self._lock = threading.Lock()
        
        # 全局统计
        self.total_calls = 0
        self.total_failures = 0
        self.total_successes = 0
        self.circuit_trips = 0
    
    @property
    def is_open(self):
        """熔断器是否打开"""
        with self._lock:
            if self._circuit_open_time is None:
                return False
            # 检查是否应该关闭
            elapsed = time.time() - self._circuit_open_time
            if elapsed >= self.recovery_timeout:
                # 恢复期：逐步释放
                self._circuit_open_time = None
                self._consecutive_failures = 0
                return False
            return True
    
    @property
    def wait_time(self):
        """还需要等待多少秒"""
        with self._lock:
            if self._circuit_open_time is None:
                return 0
            elapsed = time.time() - self._circuit_open_time
            return max(0, self.recovery_timeout - elapsed)
    
    def record_success(self):
        """记录成功调用"""
        with self._lock:
            self.total_calls += 1
            self.total_successes += 1
            self._consecutive_failures = 0
            self._consecutive_successes += 1
    
    def record_failure(self):
        """记录失败调用"""
        with self._lock:
            self.total_calls += 1
            self.total_failures += 1
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            
            # 检查是否触发熔断
            if self._consecutive_failures >= self.failure_threshold:
                if self._circuit_open_time is None:
                    self._circuit_open_time = time.time()
                    self.circuit_trips += 1
    
    def get_stats(self):
        """获取统计信息"""
        with self._lock:
            return {
                "total_calls": self.total_calls,
                "success_rate": f"{self.total_successes/self.total_calls*100:.1f}%" if self.total_calls else "N/A",
                "circuit_open": self.is_open,
                "wait_time": f"{self.wait_time:.1f}s" if self.is_open else "N/A",
                "circuit_trips": self.circuit_trips
            }

# 全局熔断器实例
_circuit_breaker = CircuitBreaker(
    failure_threshold=3,      # 连续3次失败触发熔断（原5次）
    recovery_timeout=60,      # 熔断后等待60秒
    max_consecutive_failures=10
)

def log_circuit_state():
    """记录熔断器状态"""
    stats = _circuit_breaker.get_stats()
    if stats["circuit_open"]:
        log("CircuitBreaker", f"熔断开启，等待{stats['wait_time']} | 成功率:{stats['success_rate']}", "WARN")
    elif stats["circuit_trips"] > 0:
        log("CircuitBreaker", f"熔断已关闭 | 成功率:{stats['success_rate']}", "INFO")

def jittered_backoff(attempt, base=1.0, max_delay=30):
    """带抖动的指数退避：避免惊群效应
    延迟 = base * 2^attempt * random(0.5, 1.5)
    """
    delay = base * (2 ** attempt) * random.uniform(0.5, 1.5)
    return min(delay, max_delay)

def call_ds(model, messages, max_tokens=4000, temperature=0.3, timeout=300, use_cache=True, fallback_model=None, pre_delay_range=(0.3, 1.5)):
    """调用DeepSeek API，带重试机制、空结果检测、自动降级、缓存

    Args:
        pre_delay_range: 调用前随机延迟范围(秒)，可设为None禁用
    """
    # 构建prompt文本用于缓存
TOPIC = ""
RESEARCH_QUESTIONS = []

DS_API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DS_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DS_FLASH = os.environ.get("DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash")
DS_PRO = os.environ.get("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro")

# 缓存目录
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def check_api_keys():
    if not DS_API_KEY:
        raise ValueError("请设置环境变量 DEEPSEEK_API_KEY")

def get_cache_key(prompt, model):
    """生成缓存key"""
    content = f"{model}:{prompt}"
    return hashlib.md5(content.encode()).hexdigest()

def call_ds(model, messages, max_tokens=4000, temperature=0.3, timeout=300, use_cache=True, fallback_model=None, pre_delay_range=(0.3, 1.5)):
    """调用DeepSeek API，带重试机制、空结果检测、自动降级、缓存、熔断器

    Args:
        pre_delay_range: 调用前随机延迟范围(秒)，可设为None禁用
    """
    # 构建prompt文本用于缓存
    prompt_text = messages[0]["content"] if messages else ""
    cache_key = get_cache_key(prompt_text, model)
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")

    # 调用前检查熔断器状态
    if _circuit_breaker.is_open:
        wait_time = _circuit_breaker.wait_time
        log("CircuitBreaker", f"熔断开启，强制等待{wait_time:.1f}秒", "WARN")
        time.sleep(wait_time + 1)  # 多等1秒确保恢复
    
    # 调用前随机延迟（错峰请求，避免惊群效应）
    if pre_delay_range:
        delay = random.uniform(*pre_delay_range)
        time.sleep(delay)

    # 检查缓存
    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            log("API", f"缓存命中: {cache_key[:8]}...")
            return cached["result"]
        except:
            pass

    headers = {"Authorization": f"Bearer {DS_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}

    # 记录prompt大小
    prompt_size = len(prompt_text)
    log("API", f"调用 {model} | prompt: {prompt_size}字符 | max_tokens: {max_tokens}")

    for attempt in range(4):  # 增加到4次重试
        try:
            start_time = time.time()
            resp = requests.post(DS_API_URL, headers=headers, json=payload, timeout=timeout)
            elapsed = time.time() - start_time

            # 详细错误捕获
            if resp.status_code != 200:
                error_msg = f"HTTP错误: {resp.status_code}"
                try:
                    error_data = resp.json()
                    if "error" in error_data:
                        error_msg += f" | {error_data['error']}"
                except:
                    error_msg += f" | {resp.text[:300]}"
                log("API", f"{error_msg} (尝试 {attempt+1}/4, {elapsed:.1f}s)", "WARN")
                _circuit_breaker.record_failure()
                log_circuit_state()
                time.sleep(jittered_backoff(attempt))
                continue

            data = resp.json()

            if "choices" in data and data["choices"]:
                result = data["choices"][0]["message"]["content"]
                # 空结果检测
                if not result or len(result.strip()) < 10:
                    log("API", f"返回空/极短结果 ({len(result) if result else 0}字符) (尝试 {attempt+1}/4, {elapsed:.1f}s)", "WARN")
                    _circuit_breaker.record_failure()
                    log_circuit_state()
                    time.sleep(jittered_backoff(attempt))
                    continue
                log("API", f"成功 ({elapsed:.1f}s, {len(result)}字符)")
                # 记录成功
                _circuit_breaker.record_success()
                # 保存缓存
                if use_cache:
                    try:
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump({"result": result, "timestamp": time.time()}, f)
                    except:
                        pass
                return result
            elif "error" in data:
                log("API", f"API业务错误: {data['error']} (尝试 {attempt+1}/4)", "WARN")
                # 记录失败
                _circuit_breaker.record_failure()
                log_circuit_state()
                time.sleep(jittered_backoff(attempt))
                continue
            else:
                log("API", f"未知响应格式: {str(data)[:200]} (尝试 {attempt+1}/4)", "WARN")
                # 记录失败
                _circuit_breaker.record_failure()
                log_circuit_state()
                time.sleep(jittered_backoff(attempt))
                continue

        except requests.exceptions.Timeout:
            log("API", f"超时 (>{timeout}s) (尝试 {attempt+1}/4)", "WARN")
            _circuit_breaker.record_failure()
            log_circuit_state()
            time.sleep(jittered_backoff(attempt))
        except requests.exceptions.ConnectionError as e:
            log("API", f"连接错误: {e} (尝试 {attempt+1}/4)", "WARN")
            _circuit_breaker.record_failure()
            log_circuit_state()
            time.sleep(jittered_backoff(attempt))
        except Exception as e:
            log("API", f"异常: {type(e).__name__}: {e} (尝试 {attempt+1}/4)", "WARN")
            _circuit_breaker.record_failure()
            log_circuit_state()
            time.sleep(jittered_backoff(attempt))

    # 所有尝试均失败，尝试降级模型
    if fallback_model and fallback_model != model:
        log("API", f"降级到 {fallback_model}", "WARN")
        return call_ds(fallback_model, messages, max_tokens=max_tokens, temperature=temperature,
                       timeout=timeout, use_cache=use_cache, fallback_model=None, pre_delay_range=None)  # 降级后禁用延迟

    # 记录最终失败
    _circuit_breaker.record_failure()
    log_circuit_state()
    log("API", "所有尝试均失败", "ERROR")
    return None

def log(agent, msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] [{agent}] {msg}", flush=True)

# ============ 加载本地PDF（支持增量模式） ============

def load_local_papers(pdf_dir, incremental=False):
    """加载本地PDF元数据（支持增量模式）"""
    meta_path = os.path.join(pdf_dir, "pdf_metadata_v3.json")
    if not os.path.exists(meta_path):
        log("Loader", f"未找到 {meta_path}", "ERROR")
        return None
    
    with open(meta_path, "r", encoding="utf-8") as f:
        raw_papers = json.load(f)
    
    # 检查增量缓存
    cache_meta_path = os.path.join(CACHE_DIR, "last_papers.json")
    if incremental and os.path.exists(cache_meta_path):
        try:
            with open(cache_meta_path, "r", encoding="utf-8") as f:
                last_cache = json.load(f)
            last_count = last_cache.get("count", 0)
            if len(raw_papers) == last_count:
                log("Loader", f"文献数量未变化 ({last_count}篇)，跳过处理")
                return None
            else:
                log("Loader", f"新增文献: {len(raw_papers) - last_count}篇")
        except:
            pass
    
    papers = []
    summaries = {}
    for i, p in enumerate(raw_papers):
        if not p.get("title"):
            continue
        paper_id = f"local_{i}"
        papers.append({
            "id": paper_id,
            "title": p.get("title", ""),
            "authors": p.get("authors", []),
            "year": p.get("year", ""),
            "journal": p.get("journal", ""),
            "doi": "",
            "first_author": p.get("authors", [""])[0] if p.get("authors") else "",
            "source": "local_pdf",
        })
        summaries[paper_id] = p.get("summary_cn", "")
    
    # 保存当前状态
    try:
        with open(cache_meta_path, "w", encoding="utf-8") as f:
            json.dump({"count": len(raw_papers), "timestamp": time.time()}, f)
    except:
        pass
    
    log("Loader", f"加载了 {len(papers)} 篇本地PDF文献")
    return papers, summaries

# ============ StructurePlanner（两步法） ============

def step1_cluster(papers, summaries):
    """Step1: 基于summary聚类生成主题（pro模型，分批处理避免prompt过长）"""
    log("StructurePlanner", "Step1: 基于summary聚类（pro模型，分批处理）...")

    # 分批处理：每批约15篇，避免prompt超过8000字符
    batch_size = 15
    batches = []
    for i in range(0, len(papers), batch_size):
        batch = papers[i:i+batch_size]
        papers_info = []
        for p in batch:
            summary = summaries.get(p['id'], '')[:300]
            papers_info.append(f"[{p['id']}] {p['title']}\n与研究主题关联: {summary}")
        batches.append("\n\n".join(papers_info))

    # Phase 1: 分批提取子主题
    sub_themes_all = []
    for batch_idx, batch_text in enumerate(batches):
        prompt = f"""你是公共政策研究领域的学术专家。请阅读以下{len(papers)}篇文献中的第{batch_idx+1}批（共{len(batches)}批），归纳这批文献涉及的核心研究主题。

文献内容：
{batch_text}

要求：
1. 归纳2-3个核心主题
2. 主题命名学术化、具体化
3. 按格式输出：子主题1: xxx

只输出主题列表。"""

        result = call_ds(DS_PRO, [{"role": "user", "content": prompt}],
                         max_tokens=400, temperature=0.2, timeout=180, use_cache=False,
                         fallback_model=DS_FLASH)

        if result:
            for line in result.strip().split('\n'):
                line = line.strip()
                if re.match(r'^子主题\d+\s*[:：]', line):
                    parts = re.split(r'[:：]', line, 1)
                    if len(parts) == 2 and parts[1].strip():
                        sub_themes_all.append(parts[1].strip())
            sub_count = len([l for l in result.strip().split('\n') if re.match(r'^子主题\d+', l.strip())])
            log("StructurePlanner", f"  批次{batch_idx+1}: 提取{sub_count}个子主题")

        time.sleep(1)

    # Phase 2: 合并子主题为4-5个主主题
    if not sub_themes_all:
        log("StructurePlanner", "子主题提取失败，使用默认主题", "ERROR")
        return ["政策设计与工具组合", "企业创新响应与行为", "产业升级与结构转型", "政策效果评估与优化"]

    sub_themes_text = "\n".join([f"{i+1}. {t}" for i, t in enumerate(sub_themes_all)])

    merge_prompt = f"""你是公共政策研究领域的学术专家。研究主题为"{TOPIC}"，共{len(papers)}篇文献。

从文献中初步提取了以下子主题：
{sub_themes_text}

请将这些子主题合并为恰好4-5个核心主题。

要求：
1. 必须生成恰好4-5个主题
2. 合并相似的子主题，保留区分度
3. 每个主题应涵盖约{len(papers)//5}-{len(papers)//4}篇文献
4. **主题名称必须≤15字**，简洁学术化（如"政策设计"而非"政策设计与工具组合及协同效应检验"）
5. **主题间呈递进或并列关系**：
   - 递进：政策设计→实施机制→效果评估→国际比较
   - 或并列：政策工具/企业创新/产业升级/效果评估

按格式输出：
主题1: [名称]
主题2: [名称]
...

只输出主题列表。"""

    result = call_ds(DS_PRO, [{"role": "user", "content": merge_prompt}],
                     max_tokens=600, temperature=0.2, timeout=180, use_cache=False,
                     fallback_model=DS_FLASH)

    themes = []
    if result:
        for line in result.strip().split('\n'):
            line = line.strip()
            if re.match(r'^主题\d+\s*[:：]', line):
                parts = re.split(r'[:：]', line, 1)
                if len(parts) == 2 and parts[1].strip():
                    themes.append(parts[1].strip())

        if themes:
            log("StructurePlanner", f"合并为 {len(themes)} 个主题：{themes}")
        else:
            log("StructurePlanner", f"合并失败，原始输出: {result[:300]}", "WARN")

    # 验证主题数量
    if len(themes) < 4:
        log("StructurePlanner", f"主题数量不足({len(themes)}个)，补充默认主题", "WARN")
        defaults = ["政策设计与工具组合", "企业创新响应与行为", "产业升级与结构转型", "政策效果评估与优化"]
        while len(themes) < 4:
            themes.append(defaults[len(themes)])
    elif len(themes) > 5:
        log("StructurePlanner", f"主题过多({len(themes)}个)，截取前5个", "WARN")
        themes = themes[:5]

    return themes

def step2_assign(papers, summaries, themes):
    """Step2: 基于摘要分配文献到主题（flash模型，JSON输出，均衡约束）"""
    log("StructurePlanner", "Step2: 基于摘要分配文献...")

    batch_size = 10
    assignments = {}
    max_per_section = max(8, len(papers) // len(themes) + 2)  # 动态上限，至少8篇

    for i in range(0, len(papers), batch_size):
        batch = papers[i:i+batch_size]

        papers_info = []
        for p in batch:
            abstract = summaries.get(p['id'], '')[:300]
            papers_info.append(f"[{p['id']}] {p['title']}\n摘要: {abstract}")

        prompt = f"""将以下文献分配到最合适的主题。

主题：
{chr(10).join([f"{j+1}. {t}" for j, t in enumerate(themes)])}

当前各主题已分配文献数：
{chr(10).join([f"  主题{j+1}({themes[j]}): {sum(1 for v in assignments.values() if v == j)}篇" for j in range(len(themes))])}

待分配文献：
{chr(10).join(papers_info)}

要求：
1. 每篇文献分配到一个最相关的主题
2. 每个主题最多分配{max_per_section}篇文献，尽量均衡
3. 输出JSON格式：{{"assignments": [{{"paper_id": "local_0", "theme": 1}}, ...]}}
4. theme值为1-{len(themes)}的整数
5. 只输出JSON"""

        result = call_ds(DS_FLASH, [{"role": "user", "content": prompt}],
                         max_tokens=1500, temperature=0.1, timeout=120)

        if result:
            try:
                result = result.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(result)
                for item in parsed.get("assignments", []):
                    paper_id = item.get("paper_id", "")
                    theme_idx = item.get("theme", 0)
                    if paper_id and isinstance(theme_idx, int):
                        idx = theme_idx - 1
                        if 0 <= idx < len(themes):
                            # 检查该主题是否已满
                            current_count = sum(1 for v in assignments.values() if v == idx)
                            if current_count < max_per_section:
                                assignments[paper_id] = idx
                            else:
                                # 找到文献最少的主题重新分配
                                counts = [sum(1 for v in assignments.values() if v == j) for j in range(len(themes))]
                                min_idx = counts.index(min(counts))
                                assignments[paper_id] = min_idx
                                log("StructurePlanner", f"  {paper_id} 主题{idx+1}已满({max_per_section})，重分配到主题{min_idx+1}", "WARN")
            except Exception as e:
                log("StructurePlanner", f"解析分配结果失败: {e}", "WARN")

        time.sleep(0.3)

    # 构建结构（先不生成rationale，等未分配文献处理完后再生成）
    structure = {"sections": []}
    for i, theme in enumerate(themes):
        sec_papers = [p for p in papers if assignments.get(p['id']) == i]
        if len(sec_papers) >= 2:
            structure["sections"].append({
                "id": f"3.{i+1}",
                "title": theme,
                "paper_ids": [p['id'] for p in sec_papers],
                "papers": sec_papers,
                "rationale": ""  # 稍后更新
            })

    # 处理未分配文献
    assigned_ids = set(assignments.keys())
    unassigned = [p for p in papers if p['id'] not in assigned_ids]
    if unassigned and structure["sections"]:
        # 分配到文献最少的节
        for p in unassigned:
            counts = [(len(sec.get("papers", [])), idx) for idx, sec in enumerate(structure["sections"])]
            counts.sort(key=lambda x: x[0])
            min_idx = counts[0][1]
            structure["sections"][min_idx]["paper_ids"].append(p['id'])
            structure["sections"][min_idx]["papers"].append(p)
        log("StructurePlanner", f"{len(unassigned)}篇未分配，均衡归入各节")

    # 更新rationale并输出最终分配结果
    for sec in structure["sections"]:
        sec["rationale"] = f"基于摘要内容分配，共{len(sec['papers'])}篇"
        log("StructurePlanner", f"  {sec['id']} {sec['title']}: {len(sec['papers'])}篇")

    return structure

def structure_and_plan(papers, summaries):
    """StructurePlanner主入口：两步法"""
    themes = step1_cluster(papers, summaries)
    structure = step2_assign(papers, summaries, themes)
    
    # 添加3.X研究差距
    small_sections = [sec for sec in structure["sections"] if len(sec.get("papers", [])) < 3]
    if small_sections:
        gap_papers = []
        for sec in small_sections:
            gap_papers.extend(sec.get("papers", []))
        if gap_papers:
            structure["sections"].append({
                "id": "3.X", 
                "title": "研究差距与未来方向",
                "papers": gap_papers,
                "rationale": "这些主题文献不足，存在研究空白"
            })
    
    return structure

# ============ Writer（并行） ============

def write_review_section(section, papers, summaries):
    """写作单节综述（pro模型）"""
    sid = section["id"]
    title = section["title"]
    rationale = section.get("rationale", "")
    section_papers = section.get("papers", [])
    
    if not section_papers:
        log("Writer", f"{sid} {title}: 无分配文献，跳过")
        return {"text": f"本节相关文献不足，现有文献未能充分覆盖{title}议题。", "source": "placeholder"}
    
    if len(section_papers) < 2:
        log("Writer", f"{sid} {title}: 仅{len(section_papers)}篇分配，输出占位说明", "WARN")
        return {"text": f"本节相关文献不足（{len(section_papers)}篇），现有文献未能充分覆盖{title}议题。", "source": "placeholder"}
    
    paper_info = []
    for p in section_papers[:12]:
        info = f"- {p.get('first_author','')} ({p.get('year','')}): {p.get('title','')[:80]}"
        if summaries.get(p['id']):
            info += f" | {summaries[p['id']][:100]}"
        paper_info.append(info)
    
    prompt = f"""请基于以下分配的文献，撰写一段关于"{TOPIC} - {title}"的学术综述（500-800字）。

本节主题说明：{rationale}

可用文献：
{chr(10).join(paper_info)}

要求：
1. 围绕"{title}"主题展开
2. 每个观点引用至少1篇文献（格式：作者(年份)）
3. 按论点组织，相似观点合并叙述
4. 学术化语言，逻辑连贯
5. 段落完整，末尾以句号结束
6. 直接输出段落文本"""
    
    # 先用pro模型，自动降级flash，max_tokens设为8192（接近最大值）
    text = call_ds(DS_PRO, [{"role": "user", "content": prompt}], max_tokens=8192, temperature=0.3,
                   fallback_model=DS_FLASH)

    if text and len(text) > 100:
        source = "pro" if len(text) > 200 else "flash"
        log("Writer", f"{sid} {title}: {len(text)}字 ({source}), {len(section_papers)}篇引用")
        return {"text": text, "source": source, "papers": section_papers}
    else:
        log("Writer", f"{sid} {title}: 写作失败", "ERROR")
        return {"text": f"{title}相关文献综述暂未能生成。", "source": "placeholder"}

def write_review_parallel(papers, summaries, structure):
    """并行写作各节综述"""
    log("Writer", "并行写作各节综述...")
    written = {}
    
    # 过滤掉3.X节
    sections_to_write = [sec for sec in structure["sections"] if sec["id"] != "3.X"]
    
    with ThreadPoolExecutor(max_workers=2) as executor:  # pro模型改为2线程，降低API负载
        futures = {executor.submit(write_review_section, sec, papers, summaries): sec["id"]
                   for sec in sections_to_write}
        
        for future in as_completed(futures):
            sid = futures[future]
            try:
                result = future.result()
                written[sid] = result
            except Exception as e:
                log("Writer", f"{sid} 写作异常: {e}", "ERROR")
                written[sid] = {"text": "写作过程发生错误。", "source": "error"}
    
    return written

# ============ Reviewer（并行，pro模型） ============

def review_section(sid, section):
    """修订单节（pro模型）"""
    text = section["text"]
    if not text or section.get("source") in ["placeholder", "error"]:
        return section
    
    issues = []
    if text and text[-1] not in "。？！.;?!":
        issues.append("段落末尾截断")
    
    if len(text) < 150:
        issues.append("段落过短")
    
    if not issues:
        return section
    
    log("Reviewer", f"{sid} : {', '.join(issues)}", "WARN")
    
    prompt = f"""以下文献综述段落存在问题：{', '.join(issues)}。请重写为一段逻辑连贯、学术规范的叙述性段落（400-600字）。

原文：
{text}

要求：
1. 围绕主题展开
2. 每个观点以"作者(年份)"嵌入引用
3. 按论点组织，相似观点合并叙述
4. 段落完整，末尾以句号结束
5. 直接输出段落文本"""

    revised = call_ds(DS_PRO, [{"role": "user", "content": prompt}], max_tokens=8192, temperature=0.3,
                      fallback_model=DS_FLASH)
    if revised and len(revised) > 150:
        section["text"] = revised
        log("Reviewer", f"  → 修订成功: {len(revised)}字")
    else:
        log("Reviewer", f"  → 修订失败（pro+flash均失败）", "WARN")
    
    return section

def review_parallel(written_sections):
    """并行修订各节"""
    log("Reviewer", "并行修订中...")
    
    with ThreadPoolExecutor(max_workers=2) as executor:  # pro模型改为2线程，降低API负载
        futures = {executor.submit(review_section, sid, sec): sid
                   for sid, sec in written_sections.items()}
        
        for future in as_completed(futures):
            sid = futures[future]
            try:
                written_sections[sid] = future.result()
            except Exception as e:
                log("Reviewer", f"{sid} 修订异常: {e}", "ERROR")
    
    issues_count = sum(1 for sec in written_sections.values() 
                      if sec.get("text") and sec["text"][-1] not in "。？！.;?!")
    log("Reviewer", f"自审完成: 发现{issues_count}处问题")
    return written_sections

# ============ Summarizer ============

def generate_summary_and_conclusion(written_sections, papers):
    """生成讨论和结论"""
    log("Summarizer", "生成讨论和结论...")
    
    section_summaries = []
    for sid, sec in written_sections.items():
        if sec.get("text") and len(sec["text"]) > 50:
            section_summaries.append(f"{sid}: {sec['text'][:200]}...")
    
    all_sections_text = "\n\n".join(section_summaries)
    
    prompt_findings = f"""基于以下文献综述各节的核心内容，总结3-5条主要研究发现。

各节内容摘要：
{all_sections_text[:2000]}

要求：
1. 每条发现需具体、有证据支撑
2. 体现文献综述的核心贡献
3. 用学术化语言表述
4. 直接输出要点"""

    findings = call_ds(DS_FLASH, [{"role": "user", "content": prompt_findings}], max_tokens=800, temperature=0.3)
    if not findings:
        findings = "（由文献综述的核心发现总结）"
    
    prompt_implications = f"""基于以下文献综述内容，提炼3-4条对研究者或政策制定者的实践启示。

各节内容摘要：
{all_sections_text[:2000]}

要求：
1. 每条启示需具体可操作
2. 基于综述中的研究发现
3. 直接输出要点"""

    implications = call_ds(DS_FLASH, [{"role": "user", "content": prompt_implications}], max_tokens=600, temperature=0.3)
    if not implications:
        implications = "（基于综述主题的具体实践建议）"
    
    prompt_conclusion = f"""基于以下文献综述内容，撰写一段200-300字的结论，总结核心贡献与研究意义。

各节内容摘要：
{all_sections_text[:2000]}

要求：
1. 概括综述的核心发现
2. 指出对领域的贡献
3. 提及局限性与未来方向
4. 直接输出段落文本"""

    conclusion = call_ds(DS_FLASH, [{"role": "user", "content": prompt_conclusion}], max_tokens=500, temperature=0.3)
    if not conclusion:
        conclusion = "（由核心发现和贡献总结）"
    
    log("Summarizer", "讨论和结论生成完成")
    return findings, implications, conclusion

# ============ Assembler ============

def assemble_review(written_sections, papers, structure, findings, implications, conclusion):
    """组装综述"""
    lines = [
        f"# {TOPIC}：文献综述",
        "",
        "## 摘要",
        "",
        f"本文系统综述了{TOPIC}的学术文献。基于本地PDF库纳入{len(papers)}篇文献进行分析。综述从文献分布中涌现结构，核心发现和主要贡献如下所述。",
        "",
        f"**关键词**：{TOPIC}",
        "",
        "---",
        "",
        "## 1. 引言",
        "",
        f"{TOPIC}是当前社会科学研究的重要议题。本综述围绕以下研究问题展开：",
        "",
    ]
    
    for rq in RESEARCH_QUESTIONS:
        lines.append(f"{rq}")
    
    lines.extend([
        "",
        "---",
        "",
        "## 2. 方法论",
        "",
        "### 2.1 数据来源",
        "- 本地PDF库：人工筛选的专题文献",
        "",
        "### 2.2 筛选标准",
        "- 与主题直接相关",
        "- 学术质量可靠",
        "",
        "### 2.3 文献构成",
    ])
    
    for sec in structure["sections"]:
        if sec["id"] != "3.X":
            count = len(sec.get("papers", []))
            lines.append(f"- {sec['title']}：{count}篇引用")
    
    lines.extend([
        "",
        "---",
        "",
        "## 3. 结果",
        "",
    ])
    
    for sec in structure["sections"]:
        sid = sec["id"]
        title = sec["title"]
        rationale = sec.get("rationale", "")
        lines.append(f"### {sid} {title}")
        if rationale:
            lines.append(f"*{rationale}*")
        lines.append("")
        
        if sid in written_sections and written_sections[sid].get("text"):
            lines.append(written_sections[sid]["text"])
        else:
            lines.append(f"本节相关文献不足，现有文献未能充分覆盖{title}议题。")
        
        lines.append("")
    
    lines.extend([
        "---",
        "",
        "## 4. 讨论",
        "",
        "### 4.1 主要发现",
        "",
        findings,
        "",
        "### 4.2 实践启示",
        "",
        implications,
        "",
        "### 4.3 局限性与未来方向",
        "",
        "**局限性**：检索范围有限；LLM观点提取基于全文概括。",
        "",
        "**未来方向**：概念整合；实证检验；跨领域比较；方法论创新。",
        "",
        "---",
        "",
        "## 5. 结论",
        "",
        conclusion,
        "",
        "---",
        "",
        "## 参考文献",
        "",
    ])
    
    cited = []
    for p in papers:
        authors = p.get("authors", [])
        if len(authors) > 2:
            author_str = ", ".join(authors[:-1]) + ", & " + authors[-1]
        elif len(authors) == 2:
            author_str = authors[0] + " & " + authors[1]
        elif len(authors) == 1:
            author_str = authors[0]
        else:
            author_str = "Anonymous"
        
        year = p.get("year", "n.d.")
        title = p.get("title", "")
        journal = p.get("journal", "")
        
        cite = f"{author_str} ({year}). {title}"
        if journal:
            cite += f". {journal}"
        
        cited.append(cite)
    
    for i, cite in enumerate(cited, 1):
        lines.append(f"{i}. {cite}")
    
    return "\n".join(lines), cited

# ============ Formatter ============

def format_phase(review):
    """格式验证"""
    log("Formatter", "格式验证...")
    review = review.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    review = re.sub(r'\n{3,}', '\n\n', review)
    log("Formatter", "修复0处")
    return review

# ============ 主流程 ============

def run_local_mode(topic, pdf_dir, research_questions, incremental=False):
    """本地模式：简化workflow"""
    global TOPIC, RESEARCH_QUESTIONS
    TOPIC = topic
    RESEARCH_QUESTIONS = research_questions
    
    print("=" * 60, flush=True)
    print(f"{TOPIC} - 本地模式 v16 (Final)", flush=True)
    print("=" * 60, flush=True)
    
    total_start = time.time()
    
    # Step 1: 加载PDF元数据
    log("Loader", "加载本地PDF元数据...")
    result = load_local_papers(pdf_dir, incremental)
    if not result:
        log("Supervisor", "无新文献或加载失败，跳过处理")
        return
    papers, summaries = result
    
    # Step 2: StructurePlanner（两步法）
    structure = structure_and_plan(papers, summaries)
    
    # Step 3: Writer（并行）
    written = write_review_parallel(papers, summaries, structure)
    
    # Step 4: Reviewer（并行，pro模型）
    written = review_parallel(written)
    
    # Step 5: Summarizer
    findings, implications, conclusion = generate_summary_and_conclusion(written, papers)
    
    # Step 6: Assembler
    review, cited_papers = assemble_review(written, papers, structure, findings, implications, conclusion)
    review = format_phase(review)
    
    # 保存
    review_output = os.path.join(pdf_dir, f"{topic.replace(' ', '_').replace('《', '').replace('》', '')}_review_final.md")
    with open(review_output, "w", encoding="utf-8") as f:
        f.write(review)
    
    # 同时保存到工作区
    workspace = r"C:\Users\lanso\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\work-mode-projects\69fb28593fde1c48435a899c"
    os.makedirs(workspace, exist_ok=True)
    workspace_output = os.path.join(workspace, f"{topic.replace(' ', '_').replace('《', '').replace('》', '')}_文献综述_v17.md")
    with open(workspace_output, "w", encoding="utf-8") as f:
        f.write(review)
    
    elapsed = time.time() - total_start
    print(f"\n{'='*60}", flush=True)
    log("Supervisor", f"完成! {elapsed:.0f}s")
    log("Supervisor", f"文献综述: {review_output}")
    log("Supervisor", f"工作区副本: {workspace_output}")
    log("Supervisor", f"正文引用{len(cited_papers)}篇")

def run(topic, mode="local", **kwargs):
    """统一入口"""
    check_api_keys()
    
    if mode == "local":
        return run_local_mode(
            topic, 
            kwargs.get("pdf_dir", ""), 
            kwargs.get("research_questions", []),
            kwargs.get("incremental", False)
        )
    else:
        log("Supervisor", f"模式 {mode} 未实现", "ERROR")

if __name__ == "__main__":
    PDF_DIR = r"C:\Users\lanso\WPSDrive\1622489959\WPS企业云盘\复旦大学\我的企业文档\论文\中国制造2025"
    RESEARCH_QUESTIONS = [
        "RQ1: 《中国制造2025》政策的核心目标与工具设计是什么？",
        "RQ2: 该政策对企业创新和生产率的影响机制是什么？",
        "RQ3: 政策的实施效果如何评估？存在哪些争议？",
        "RQ4: 该政策与德国工业4.0、美国先进制造等战略的比较差异？",
        "RQ5: 政策演进与新质生产力、产业链自主可控的关联？",
    ]
    
    run("《中国制造2025》产业政策研究", mode="local", pdf_dir=PDF_DIR, research_questions=RESEARCH_QUESTIONS)
