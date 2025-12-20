# requirements.txt
"""
gradio>=4.0.0
python-docx>=0.8.11
Pillow>=10.0.0
paddleocr>=2.7.0
paddlepaddle>=2.5.0
requests>=2.31.0
opencv-python>=4.8.0
"""

# main.py
import gradio as gr
import docx
from docx.document import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph
from PIL import Image
from paddleocr import PaddleOCR
import io
import os
import json
import requests
from typing import List, Dict, Tuple
from pathlib import Path
import tempfile
import shutil


class TestReportAnalyzer:
    def __init__(self):
        # 初始化OCR模型
        self.ocr = PaddleOCR(use_angle_cls=True, lang='ch', use_gpu=False)
        self.ollama_url = "http://localhost:11434/api/generate"
        self.model_name = "qwen2.5:7b"  # 可修改为你的模型名称

    def extract_images_from_docx(self, doc_path: str, output_dir: str) -> List[Tuple[str, int]]:
        """从Word文档中提取所有图片"""
        doc = docx.Document(doc_path)
        image_list = []

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # 提取图片
        for rel in doc.part.rels.values():
            if "image" in rel.target_ref:
                img_data = rel.target_part.blob
                img_name = os.path.basename(rel.target_ref)
                img_path = os.path.join(output_dir, img_name)

                with open(img_path, 'wb') as f:
                    f.write(img_data)

                # 找到图片在文档中的位置(段落索引)
                para_index = self._find_image_paragraph(doc, rel.rId)
                image_list.append((img_path, para_index))

        return sorted(image_list, key=lambda x: x[1])

    def _find_image_paragraph(self, doc: Document, rel_id: str) -> int:
        """找到图片所在的段落索引"""
        for idx, paragraph in enumerate(doc.paragraphs):
            if rel_id in paragraph._element.xml:
                return idx
        return -1

    def ocr_images(self, image_paths: List[str], progress=gr.Progress()) -> Dict[str, str]:
        """对所有图片进行OCR识别"""
        ocr_results = {}

        for idx, img_path in enumerate(image_paths):
            progress((idx + 1) / len(image_paths), desc=f"OCR识别中 {idx + 1}/{len(image_paths)}")

            try:
                result = self.ocr.ocr(img_path, cls=True)

                # 提取文本
                text_lines = []
                if result and result[0]:
                    for line in result[0]:
                        text_lines.append(line[1][0])

                ocr_results[img_path] = "\n".join(text_lines)
            except Exception as e:
                ocr_results[img_path] = f"OCR识别失败: {str(e)}"

        return ocr_results

    def parse_docx_structure(self, doc_path: str) -> List[Dict]:
        """解析Word文档结构,按大标题组织内容"""
        doc = docx.Document(doc_path)
        sections = []
        current_section = None

        for idx, paragraph in enumerate(doc.paragraphs):
            text = paragraph.text.strip()

            if not text:
                continue

            # 检测大标题(根据样式判断,可能需要根据实际情况调整)
            is_heading = paragraph.style.name.startswith('Heading') or \
                         (paragraph.runs and paragraph.runs[0].bold and len(text) < 50)

            if is_heading and len(text) > 0:
                # 保存上一个section
                if current_section:
                    sections.append(current_section)

                # 创建新section
                current_section = {
                    'title': text,
                    'content': [],
                    'paragraph_range': [idx, idx]
                }
            elif current_section:
                current_section['content'].append({
                    'type': 'text',
                    'content': text,
                    'paragraph_index': idx
                })
                current_section['paragraph_range'][1] = idx

        if current_section:
            sections.append(current_section)

        return sections

    def integrate_ocr_results(self, sections: List[Dict], image_list: List[Tuple[str, int]],
                              ocr_results: Dict[str, str]) -> List[Dict]:
        """将OCR结果整合到对应的section中"""
        for img_path, para_idx in image_list:
            ocr_text = ocr_results.get(img_path, "")

            # 找到图片所属的section
            for section in sections:
                if section['paragraph_range'][0] <= para_idx <= section['paragraph_range'][1]:
                    section['content'].append({
                        'type': 'image_ocr',
                        'image_path': img_path,
                        'ocr_text': ocr_text,
                        'paragraph_index': para_idx
                    })
                    break

        # 按段落索引排序内容
        for section in sections:
            section['content'].sort(key=lambda x: x.get('paragraph_index', 0))

        return sections

    def analyze_section_with_ollama(self, section: Dict, progress=None) -> Dict:
        """使用Ollama分析单个section"""
        # 构建分析prompt
        section_text = f"# {section['title']}\n\n"

        for item in section['content']:
            if item['type'] == 'text':
                section_text += f"{item['content']}\n\n"
            elif item['type'] == 'image_ocr':
                section_text += f"[图片内容识别]:\n{item['ocr_text']}\n\n"

        prompt = f"""你是一个专业的BMC RAS功能测试审查专家。请仔细分析以下测试记录,找出潜在的问题。

测试内容:
{section_text}

请从以下维度进行分析:
1. **测试结果一致性**: 检查测试结果是否与预期一致,是否有矛盾之处
2. **错误日志分析**: 查看是否有异常错误、警告或失败信息
3. **数据完整性**: 检查测试数据是否完整(如遍历测试中每个项目是否都有记录)
4. **对比分析**: 对比相邻测试用例,查找异常差异(如丝印缺失、参数不一致等)
5. **寄存器/诊断信息**: 检查寄存器值、诊断日志是否正常
6. **边界条件**: 检查是否测试了边界情况

请以结构化的方式输出分析结果:
- 如果发现问题,请明确指出问题类型、位置和严重程度(高/中/低)
- 如果没有发现问题,说明"未发现明显问题"
- 提供改进建议

分析结果:"""

        try:
            # 调用Ollama API
            response = requests.post(
                self.ollama_url,
                json={
                    "model": self.model_name,
                    "prompt": prompt,
                    "stream": False
                },
                timeout=300
            )

            if response.status_code == 200:
                result = response.json()
                analysis = result.get('response', '分析失败')
            else:
                analysis = f"API调用失败: {response.status_code}"

        except Exception as e:
            analysis = f"分析出错: {str(e)}"

        return {
            'section_title': section['title'],
            'analysis': analysis
        }

    def generate_report(self, analyses: List[Dict]) -> str:
        """生成最终报告"""
        report = "# BMC RAS功能测试报告自动审查结果\n\n"
        report += "---\n\n"

        # 统计问题
        total_sections = len(analyses)
        issues_found = sum(1 for a in analyses if '未发现明显问题' not in a['analysis'])

        report += f"## 总体概况\n\n"
        report += f"- 测试模块总数: {total_sections}\n"
        report += f"- 发现潜在问题的模块: {issues_found}\n"
        report += f"- 审查通过率: {((total_sections - issues_found) / total_sections * 100):.1f}%\n\n"
        report += "---\n\n"

        # 详细分析结果
        report += "## 详细分析结果\n\n"

        for idx, analysis in enumerate(analyses, 1):
            report += f"### {idx}. {analysis['section_title']}\n\n"
            report += f"{analysis['analysis']}\n\n"
            report += "---\n\n"

        return report


def process_test_report(docx_file, ollama_model, progress=gr.Progress()):
    """主处理函数"""
    if docx_file is None:
        return "请上传Word测试报告文件"

    try:
        # 创建临时目录
        temp_dir = tempfile.mkdtemp()
        image_dir = os.path.join(temp_dir, "images")

        progress(0, desc="初始化...")
        analyzer = TestReportAnalyzer()
        analyzer.model_name = ollama_model

        # 1. 提取图片
        progress(0.1, desc="提取图片...")
        image_list = analyzer.extract_images_from_docx(docx_file.name, image_dir)

        # 2. OCR识别
        progress(0.2, desc="OCR识别中...")
        image_paths = [img[0] for img in image_list]
        ocr_results = analyzer.ocr_images(image_paths, progress)

        # 3. 解析文档结构
        progress(0.5, desc="解析文档结构...")
        sections = analyzer.parse_docx_structure(docx_file.name)

        # 4. 整合OCR结果
        progress(0.6, desc="整合OCR结果...")
        sections = analyzer.integrate_ocr_results(sections, image_list, ocr_results)

        # 5. AI分析每个section
        progress(0.7, desc="AI分析中...")
        analyses = []
        for idx, section in enumerate(sections):
            progress(0.7 + 0.2 * (idx / len(sections)),
                     desc=f"分析模块 {idx + 1}/{len(sections)}: {section['title'][:20]}...")
            analysis = analyzer.analyze_section_with_ollama(section)
            analyses.append(analysis)

        # 6. 生成报告
        progress(0.95, desc="生成报告...")
        final_report = analyzer.generate_report(analyses)

        # 清理临时文件
        shutil.rmtree(temp_dir)

        progress(1.0, desc="完成!")
        return final_report

    except Exception as e:
        return f"处理出错: {str(e)}\n\n请检查:\n1. Word文件格式是否正确\n2. Ollama服务是否运行(http://localhost:11434)\n3. 模型是否已下载"


# 创建Gradio界面
def create_ui():
    with gr.Blocks(title="BMC RAS测试报告自动审查系统", theme=gr.themes.Soft()) as demo:
        gr.Markdown("""
        # 🔍 BMC RAS测试报告自动审查系统

        本系统使用OCR + AI技术自动审查测试报告,发现潜在问题

        ### 使用步骤:
        1. 上传Word格式的测试报告(.docx)
        2. 选择或输入Ollama模型名称
        3. 点击"开始分析"按钮
        4. 等待分析完成,查看审查报告
        """)

        with gr.Row():
            with gr.Column(scale=1):
                docx_input = gr.File(
                    label="上传测试报告",
                    file_types=[".docx"],
                    type="filepath"
                )

                model_input = gr.Textbox(
                    label="Ollama模型名称",
                    value="qwen2.5:7b",
                    placeholder="例如: qwen2.5:7b, llama2:13b"
                )

                analyze_btn = gr.Button("🚀 开始分析", variant="primary", size="lg")

                gr.Markdown("""
                ### ⚙️ 系统要求:
                - 已安装Ollama并运行在本地
                - 已下载对应的AI模型
                - Word文档格式正确

                ### 📝 分析维度:
                - ✅ 测试结果一致性
                - 🔍 错误日志分析
                - 📊 数据完整性检查
                - 🔄 相邻用例对比
                - 📈 寄存器/诊断信息
                - ⚠️ 边界条件测试
                """)

            with gr.Column(scale=2):
                output = gr.Markdown(
                    label="审查报告",
                    value="等待上传文件并开始分析..."
                )

        analyze_btn.click(
            fn=process_test_report,
            inputs=[docx_input, model_input],
            outputs=output
        )

        gr.Markdown("""
        ---
        ### 💡 使用提示:
        - 首次运行需要下载OCR模型,请耐心等待
        - 分析时间取决于报告大小和图片数量
        - 建议使用参数量7B以上的模型以获得更好的分析效果
        - 确保Ollama服务正常运行: `ollama serve`
        """)

    return demo


if __name__ == "__main__":
    demo = create_ui()
    demo.launch(
        server_name="127.0.0.1",  # 改为 127.0.0.1
        server_port=7860,
        share=False,
        inbrowser=True  # 自动打开浏览器
    )
