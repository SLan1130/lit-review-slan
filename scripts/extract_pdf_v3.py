"""
提取PDF全文并生成500字中文核心概括
v3简化：只输出与研究主题相关的中文summary（500字内）
"""
import os, json, time, concurrent.futures, requests

PDF_DIR = r"C:\Users\lanso\WPSDrive\1622489959\WPS企业云盘\复旦大学\我的企业文档\论文\中国制造2025"
DS_API_URL = "https://api.deepseek.com/chat/completions"
DS_API_KEY = "sk-c3b2538d69534e558c0584e0750f354f"
DS_FLASH = "deepseek-v4-flash"

def call_ds(model, messages, max_tokens=4000, temperature=0.3):
    headers = {"Authorization": f"Bearer {DS_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    try:
        resp = requests.post(DS_API_URL, headers=headers, json=payload, timeout=120)
        data = resp.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"API Error: {e}")
    return None

def extract_full_text(pdf_path, max_chars=12000):
    """提取PDF全文"""
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
            if len(text) > max_chars:
                break
        return text[:max_chars]
    except Exception as e:
        print(f"PDF读取失败 {os.path.basename(pdf_path)}: {e}")
        return ""

def analyze_paper(pdf_path, topic="《中国制造2025》产业政策"):
    """分析单篇PDF：只输出中文核心概括"""
    filename = os.path.basename(pdf_path)
    print(f"处理: {filename}")
    
    full_text = extract_full_text(pdf_path)
    if not full_text:
        return None
    
    # 一步完成：提取元数据+中文概括
    prompt = f"""请基于以下学术论文的全文内容，完成两个任务：

任务1：提取元数据（JSON格式）
{{
  "title": "论文完整标题",
  "authors": ["作者1", "作者2"],
  "year": "发表年份",
  "journal": "期刊名称"
}}

任务2：用中文概括该论文与"{topic}"研究主题的相关内容（400-500字）
要求：
1. 核心研究问题是什么？
2. 用了什么方法？
3. 主要发现/结论是什么？
4. 与"{topic}"主题的理论或实证关联？
5. 学术化语言，直接输出段落文本

全文内容（前10000字）：
{full_text[:10000]}

输出格式：
先输出JSON元数据，然后空一行，输出中文概括段落。"""

    result = call_ds(DS_FLASH, [{"role": "user", "content": prompt}], 
                     max_tokens=4000, temperature=0.2)
    
    if not result:
        return None
    
    # 解析JSON和中文概括
    metadata = {"filename": filename, "pdf_path": pdf_path, "title": "", "authors": [], "year": "", "journal": ""}
    summary_cn = ""
    
    try:
        # 尝试提取JSON
        json_match = result.find('{')
        json_end = result.find('}', json_match) + 1
        if json_match >= 0 and json_end > json_match:
            json_str = result[json_match:json_end]
            parsed = json.loads(json_str)
            metadata.update(parsed)
        
        # 提取JSON之后的内容作为summary
        summary_cn = result[json_end:].strip()
    except:
        # 如果解析失败，把整个结果作为summary
        summary_cn = result
    
    metadata["summary_cn"] = summary_cn[:600]  # 限制600字
    
    time.sleep(0.3)
    return metadata

def main():
    pdf_files = [os.path.join(PDF_DIR, f) for f in os.listdir(PDF_DIR) if f.lower().endswith('.pdf')]
    print(f"找到 {len(pdf_files)} 篇PDF")
    
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(analyze_paper, pdf): pdf for pdf in pdf_files}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
    
    output_path = os.path.join(PDF_DIR, "pdf_metadata_v3.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！提取了 {len(results)}/{len(pdf_files)} 篇文献")
    print(f"结果保存到: {output_path}")

if __name__ == "__main__":
    main()
