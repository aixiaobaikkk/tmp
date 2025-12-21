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
from paddleocr import PaddleOCR
import requests
import tempfile
import shutil
import os
import json
import re
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, asdict
from enum import Enum
import traceback


class ComponentType(Enum):
    """部件类型枚举"""
    CPU = "CPU"
    MEMORY = "Memory"
    PCIE = "PCIe"
    SATA = "SATA"
    OTHER = "Other"


class ErrorType(Enum):
    """错误类型枚举"""
    CORRECTABLE = "Correctable"
    UNCORRECTABLE = "Uncorrectable"
    FATAL = "Fatal"
    OTHER = "Other"


@dataclass
class TestCase:
    """测试用例数据结构"""
    title: str
    component: ComponentType
    error_type: ErrorType
    test_result: str  # PASS/FAIL
    raw_text: str
    ocr_images: List[Dict]
    register_values: Dict[str, str] = None
    silk_screen: str = None
    error_log: str = None
    diagnostic_info: str = None
    position_info: str = None
    
    def __post_init__(self):
        if self.register_values is None:
            self.register_values = {}
        if self.ocr_images is None:
            self.ocr_images = []


class BMCRASKnowledgeBase:
    """BMC RAS专业知识库"""
    
    REGISTER_PATTERNS = {
        'CPU': {
            'MCA_BANKS': r'MCA_BANK\d+',
            'MCi_STATUS': r'MCi_STATUS.*?0x[0-9a-fA-F]+',
            'MCi_ADDR': r'MCi_ADDR.*?0x[0-9a-fA-F]+',
        },
        'Memory': {
            'DIMM_SLOT': r'DIMM[A-Z]?\d+',
            'CE_COUNT': r'CE.*?count.*?\d+',
            'UE_COUNT': r'UE.*?count.*?\d+',
        },
        'PCIe': {
            'BDF': r'[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]',
            'AER': r'AER.*?register',
        }
    }
    
    EXPECTED_FIELDS = {
        'CPU': ['MCA Bank', 'Error Type', 'MCi_STATUS', 'Processor ID'],
        'Memory': ['DIMM Slot', 'Error Address', 'CE/UE Count', 'ECC Status'],
        'PCIe': ['BDF', 'AER Status', 'Device ID'],
        'SATA': ['Port Number', 'Device Status', 'Error Log'],
    }


class EnhancedTestReportAnalyzer:
    def __init__(self, ollama_url: str = "http://localhost:11434/api/generate", 
                 model_name: str = "qwen2.5:7b"):
        try:
            self.ocr = PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
        except Exception as e:
            print(f"OCR初始化警告: {e}")
            self.ocr = PaddleOCR(lang='ch', show_log=False)
        
        self.ollama_url = ollama_url
        self.model_name = model_name
        self.knowledge_base = BMCRASKnowledgeBase()
        
    def extract_images_from_docx(self, doc_path: str, output_dir: str) -> List[Tuple[str, int]]:
        """从Word文档中提取图片"""
        doc = docx.Document(doc_path)
        image_list = []
        os.makedirs(output_dir, exist_ok=True)
        
        for rel in doc.part.rels.values():
            if "image" in rel.target_ref:
                img_data = rel.target_part.blob
                img_name = os.path.basename(rel.target_ref)
                img_path = os.path.join(output_dir, img_name)
                
                with open(img_path, 'wb') as f:
                    f.write(img_data)
                
                para_index = self._find_image_paragraph(doc, rel.rId)
                image_list.append((img_path, para_index))
        
        return sorted(image_list, key=lambda x: x[1])
    
    def _find_image_paragraph(self, doc, rel_id: str) -> int:
        for idx, paragraph in enumerate(doc.paragraphs):
            if rel_id in paragraph._element.xml:
                return idx
        return -1
    
    def ocr_images(self, image_paths: List[str], progress=None) -> Dict[str, Dict]:
        """OCR识别图片"""
        ocr_results = {}
        
        for idx, img_path in enumerate(image_paths):
            if progress:
                progress((idx + 1) / len(image_paths), 
                        desc=f"OCR识别 {idx + 1}/{len(image_paths)}")
            
            try:
                result = self.ocr.ocr(img_path, cls=True)
                text_lines = []
                
                if result and result[0]:
                    for line in result[0]:
                        if line and len(line) >= 2:
                            text_lines.append(line[1][0])
                
                full_text = "\n".join(text_lines)
                
                ocr_results[img_path] = {
                    'text': full_text,
                    'lines': text_lines,
                    'register_values': self._extract_registers(full_text),
                    'silk_screen': self._extract_silk_screen(full_text),
                }
                
            except Exception as e:
                ocr_results[img_path] = {
                    'text': f"OCR失败: {str(e)}",
                    'lines': [],
                    'register_values': {},
                    'silk_screen': None,
                }
        
        return ocr_results
    
    def _extract_registers(self, text: str) -> Dict[str, str]:
        """提取寄存器值"""
        registers = {}
        patterns = [
            r'(MCA_BANK\d+|MCi_\w+)\s*[=:]\s*(0x[0-9a-fA-F]+)',
            r'(DIMM[A-Z]?\d+)\s*[=:]\s*([^\s]+)',
            r'([A-Z_]+_REG(?:ISTER)?)\s*[=:]\s*(0x[0-9a-fA-F]+|\d+)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                registers[match[0]] = match[1]
        
        return registers
    
    def _extract_silk_screen(self, text: str) -> Optional[str]:
        """提取丝印信息"""
        patterns = [
            r'(silk.*?screen|丝印)[:\s]+([A-Z0-9_-]+)',
            r'(label|标签)[:\s]+([A-Z0-9_-]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(2)
        return None
    
    def parse_test_structure(self, doc_path: str) -> List[TestCase]:
        """解析测试报告结构"""
        doc = docx.Document(doc_path)
        test_cases = []
        current_case = None
        current_text = []
        
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            
            is_test_title = self._is_test_case_title(text, para)
            
            if is_test_title:
                if current_case:
                    current_case.raw_text = "\n".join(current_text)
                    test_cases.append(current_case)
                
                current_case = TestCase(
                    title=text,
                    component=self._identify_component(text),
                    error_type=self._identify_error_type(text),
                    test_result=self._extract_test_result(text),
                    raw_text="",
                    ocr_images=[]
                )
                current_text = [text]
            elif current_case:
                current_text.append(text)
        
        if current_case:
            current_case.raw_text = "\n".join(current_text)
            test_cases.append(current_case)
        
        return test_cases
    
    def _is_test_case_title(self, text: str, para) -> bool:
        is_heading = para.style.name.startswith('Heading')
        is_bold = para.runs and para.runs[0].bold if para.runs else False
        has_keywords = any(kw in text.lower() for kw in 
                          ['test', '测试', 'cpu', 'memory', 'pcie', 'sata', 'inject'])
        return (is_heading or (is_bold and len(text) < 150)) and has_keywords
    
    def _identify_component(self, text: str) -> ComponentType:
        text_lower = text.lower()
        if 'cpu' in text_lower or 'processor' in text_lower:
            return ComponentType.CPU
        elif 'mem' in text_lower or 'dimm' in text_lower:
            return ComponentType.MEMORY
        elif 'pcie' in text_lower:
            return ComponentType.PCIE
        elif 'sata' in text_lower or 'disk' in text_lower:
            return ComponentType.SATA
        return ComponentType.OTHER
    
    def _identify_error_type(self, text: str) -> ErrorType:
        text_lower = text.lower()
        if 'ce' in text_lower or 'correct' in text_lower:
            return ErrorType.CORRECTABLE
        elif 'uce' in text_lower or 'uncorrect' in text_lower:
            return ErrorType.UNCORRECTABLE
        elif 'fatal' in text_lower:
            return ErrorType.FATAL
        return ErrorType.OTHER
    
    def _extract_test_result(self, text: str) -> str:
        if 'pass' in text.lower():
            return "PASS"
        elif 'fail' in text.lower():
            return "FAIL"
        return "UNKNOWN"
    
    def integrate_ocr_to_testcases(self, test_cases: List[TestCase], 
                                   image_list: List[Tuple[str, int]], 
                                   ocr_results: Dict[str, Dict]) -> List[TestCase]:
        """整合OCR结果到测试用例"""
        for img_path, para_idx in image_list:
            ocr_data = ocr_results.get(img_path, {})
            
            for test_case in test_cases:
                test_case.ocr_images.append({
                    'path': img_path,
                    'para_idx': para_idx,
                    'ocr_data': ocr_data
                })
                
                if ocr_data.get('register_values'):
                    test_case.register_values.update(ocr_data['register_values'])
                
                if ocr_data.get('silk_screen') and not test_case.silk_screen:
                    test_case.silk_screen = ocr_data['silk_screen']
        
        return test_cases
    
    def extract_structured_info(self, test_case: TestCase) -> Dict:
        """第一轮推理：提取结构化信息"""
        prompt = f"""你是BMC RAS测试数据提取专家。从以下内容中提取关键信息。

测试标题: {test_case.title}
部件: {test_case.component.value}
错误类型: {test_case.error_type.value}

文本内容:
{test_case.raw_text[:800]}

OCR图片内容:
"""
        
        for idx, img in enumerate(test_case.ocr_images[:2]):
            ocr_text = img['ocr_data'].get('text', '')[:500]
            prompt += f"\n图片{idx+1}:\n{ocr_text}\n"
        
        prompt += """
请提取并以JSON格式输出:
{
    "registers": {"寄存器名": "值"},
    "silk_screen": "丝印信息",
    "error_log": "错误日志摘要",
    "diagnosis": "诊断结果",
    "position": "部件位置"
}
"""
        
        try:
            response = self._call_ollama(prompt, timeout=120)
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return {'raw_response': response}
        except:
            return {'error': '提取失败'}
    
    def analyze_single_testcase(self, test_case: TestCase, structured_info: Dict) -> Dict:
        """第二轮推理：分析单个测试用例"""
        prompt = f"""你是BMC RAS功能测试审查专家。审查以下测试用例。

测试: {test_case.title}
部件: {test_case.component.value}
错误类型: {test_case.error_type.value}
结果: {test_case.test_result}

提取信息:
{json.dumps(structured_info, indent=2, ensure_ascii=False)}

原始文本:
{test_case.raw_text[:600]}

审查重点:
1. 寄存器值是否正确？
2. 丝印信息是否完整？
3. 诊断结果是否准确？
4. 错误日志是否完整？
5. 部件位置是否准确？

输出格式:
**问题**: (如有)
- [严重度] 具体问题描述
**证据**: 支持数据
**建议**: 改进建议

无问题则输出: "未发现明显问题"
"""
        
        try:
            response = self._call_ollama(prompt, timeout=180)
            return {
                'test_case_title': test_case.title,
                'analysis': response,
                'has_issues': '未发现明显问题' not in response
            }
        except Exception as e:
            return {
                'test_case_title': test_case.title,
                'analysis': f'分析失败: {str(e)}',
                'has_issues': False
            }
    
    def cross_testcase_analysis(self, test_cases: List[TestCase], 
                               individual_analyses: List[Dict]) -> Dict:
        """第三轮推理：跨测试用例关联分析"""
        by_component = {}
        for tc in test_cases:
            comp = tc.component.value
            if comp not in by_component:
                by_component[comp] = []
            by_component[comp].append(tc)
        
        prompt = f"""你是BMC RAS测试总体审查专家。进行关联分析。

测试总数: {len(test_cases)}
部件分布: {', '.join([f'{k}: {len(v)}个' for k, v in by_component.items()])}

各部件概况:
"""
        
        for comp, cases in list(by_component.items())[:5]:
            prompt += f"\n{comp}:\n"
            for tc in cases[:3]:
                prompt += f"  - {tc.title[:50]} [{tc.test_result}]\n"
        
        prompt += "\n单项分析摘要:\n"
        for analysis in individual_analyses[:5]:
            prompt += f"\n{analysis['test_case_title'][:40]}:\n{analysis['analysis'][:150]}...\n"
        
        prompt += """
关联分析要点:
1. 同部件不同错误类型对比
2. 相似测试一致性
3. 丝印连续性检查
4. 测试覆盖度评估

输出:
**关联问题**: 发现的跨测试问题
**一致性评估**: 相似测试对比结果
**风险等级**: 高/中/低
**建议**: 整体改进建议
"""
        
        try:
            response = self._call_ollama(prompt, timeout=300)
            return {'cross_analysis': response, 'component_summary': by_component}
        except Exception as e:
            return {'cross_analysis': f'关联分析失败: {str(e)}', 'component_summary': by_component}
    
    def _call_ollama(self, prompt: str, timeout: int = 300) -> str:
        """调用Ollama API"""
        response = requests.post(
            self.ollama_url,
            json={"model": self.model_name, "prompt": prompt, "stream": False},
            timeout=timeout
        )
        
        if response.status_code == 200:
            return response.json().get('response', '生成失败')
        raise Exception(f"API错误: {response.status_code}")
    
    def generate_comprehensive_report(self, test_cases: List[TestCase],
                                     individual_analyses: List[Dict],
                                     cross_analysis: Dict) -> str:
        """生成综合报告"""
        report = "# 🔬 BMC RAS功能测试报告 - 深度审查分析\n\n"
        report += "---\n\n"
        
        total_cases = len(test_cases)
        passed_cases = sum(1 for tc in test_cases if tc.test_result == "PASS")
        issues_found = sum(1 for a in individual_analyses if a.get('has_issues', False))
        
        report += "## 📊 执行摘要\n\n"
        report += f"- **测试用例总数**: {total_cases}\n"
        report += f"- **PASS数量**: {passed_cases} ({passed_cases/total_cases*100:.1f}%)\n"
        report += f"- **发现问题的用例**: {issues_found}\n"
        report += f"- **问题发现率**: {issues_found/total_cases*100:.1f}%\n\n"
        
        comp_stats = {}
        for tc in test_cases:
            comp = tc.component.value
            comp_stats[comp] = comp_stats.get(comp, 0) + 1
        
        report += "### 部件测试覆盖度\n\n"
        for comp, count in sorted(comp_stats.items()):
            report += f"- **{comp}**: {count} 个测试\n"
        report += "\n---\n\n"
        
        report += "## 🔍 单项测试详细分析\n\n"
        for idx, analysis in enumerate(individual_analyses, 1):
            report += f"### {idx}. {analysis['test_case_title']}\n\n"
            if analysis.get('has_issues'):
                report += "⚠️ **发现问题**\n\n"
            else:
                report += "✅ **通过审查**\n\n"
            report += f"{analysis['analysis']}\n\n---\n\n"
        
        report += "## 🔗 跨测试用例关联分析\n\n"
        report += cross_analysis.get('cross_analysis', '关联分析未执行')
        report += "\n\n---\n\n"
        
        report += "## 📋 总体结论\n\n"
        if issues_found > total_cases * 0.3:
            report += "⚠️ **风险等级**: 高\n\n"
        elif issues_found > total_cases * 0.1:
            report += "⚡ **风险等级**: 中\n\n"
        else:
            report += "✅ **风险等级**: 低\n\n"
        
        report += "### 建议措施\n\n"
        report += "1. 复测所有高严重度问题用例\n"
        report += "2. 补充缺失的测试场景\n"
        report += "3. 优化测试流程和文档规范\n\n"
        
        return report


def process_enhanced_report(docx_file, ollama_model, max_cases, progress=gr.Progress()):
    """主处理函数"""
    if docx_file is None:
        return "❌ 请上传Word测试报告"
    
    try:
        temp_dir = tempfile.mkdtemp()
        image_dir = os.path.join(temp_dir, "images")
        
        progress(0, desc="🚀 初始化...")
        analyzer = EnhancedTestReportAnalyzer(model_name=ollama_model)
        
        progress(0.1, desc="📷 提取图片...")
        image_list = analyzer.extract_images_from_docx(docx_file.name, image_dir)
        
        if not image_list:
            return "⚠️ 未找到图片"
        
        progress(0.2, desc="🔍 OCR识别...")
        image_paths = [img[0] for img in image_list]
        ocr_results = analyzer.ocr_images(image_paths, progress)
        
        progress(0.4, desc="📋 解析测试用例...")
        test_cases = analyzer.parse_test_structure(docx_file.name)
        
        if not test_cases:
            return "⚠️ 未识别到测试用例"
        
        progress(0.5, desc="🔗 整合数据...")
        test_cases = analyzer.integrate_ocr_to_testcases(test_cases, image_list, ocr_results)
        
        # 限制处理数量
        test_cases_to_analyze = test_cases[:max_cases]
        
        progress(0.6, desc="🧠 提取结构化信息...")
        structured_infos = []
        for idx, tc in enumerate(test_cases_to_analyze):
            progress(0.6 + 0.1 * (idx / len(test_cases_to_analyze)),
                    desc=f"提取 {idx+1}/{len(test_cases_to_analyze)}")
            info = analyzer.extract_structured_info(tc)
            structured_infos.append(info)
        
        progress(0.7, desc="🔬 分析单个测试...")
        individual_analyses = []
        for idx, (tc, info) in enumerate(zip(test_cases_to_analyze, structured_infos)):
            progress(0.7 + 0.15 * (idx / len(test_cases_to_analyze)),
                    desc=f"分析 {idx+1}/{len(test_cases_to_analyze)}")
            analysis = analyzer.analyze_single_testcase(tc, info)
            individual_analyses.append(analysis)
        
        progress(0.9, desc="🔗 关联分析...")
        cross_analysis = analyzer.cross_testcase_analysis(test_cases_to_analyze, individual_analyses)
        
        progress(0.95, desc="📄 生成报告...")
        final_report = analyzer.generate_comprehensive_report(
            test_cases_to_analyze, individual_analyses, cross_analysis)
        
        shutil.rmtree(temp_dir)
        progress(1.0, desc="✅ 完成!")
        
        return final_report
        
    except Exception as e:
        error_detail = traceback.format_exc()
        return f"## ❌ 处理出错\n\n{str(e)}\n\n```\n{error_detail}\n```"


def create_ui():
    with gr.Blocks(title="BMC RAS测试报告深度分析系统", theme=gr.themes.Soft()) as demo:
        gr.Markdown("""
        # 🔬 BMC RAS测试报告深度分析系统
        
        **三轮AI推理 + 跨测试关联分析**
        
        ### 分析流程:
        1. **OCR识别** → 提取图片中的寄存器、丝印、日志
        2. **结构化提取** → AI第一轮：提取关键字段
        3. **单项分析** → AI第二轮：审查每个测试用例
        4. **关联分析** → AI第三轮：跨测试用例对比
        5. **综合报告** → 生成问题清单和改进建议
        """)
        
        with gr.Row():
            with gr.Column(scale=1):
                docx_input = gr.File(
                    label="📄 上传测试报告 (.docx)",
                    file_types=[".docx"],
                    type="filepath"
                )
                
                model_input = gr.Textbox(
                    label="🤖 Ollama模型",
                    value="qwen2.5:7b",
                    placeholder="qwen2.5:7b, deepseek-r1:7b"
                )
                
                max_cases_input = gr.Slider(
                    label="最大分析用例数",
                    minimum=5,
                    maximum=50,
                    value=10,
                    step=5,
                    info="分析前N个测试用例（更多用例需要更长时间）"
                )
                
                analyze_btn = gr.Button("🚀 开始深度分析", variant="primary", size="lg")
                
                gr.Markdown("""
                ### ⚙️ 系统要求:
                - ✅ Ollama运行中 (`ollama serve`)
                - ✅ 模型已下载 (`ollama pull qwen2.5:7b`)
                - ✅ Python依赖已安装
                
                ### 🎯 分析重点:
                - 🔍 寄存器置位正确性
                - 📌 丝印信息完整性
                - 📊 诊断结果准确性
                - 🔗 跨测试一致性对比
                - 📈 部件测试覆盖度
                - ⚠️ 边界条件检查
                """)
            
            with gr.Column(scale=2):
                output = gr.Markdown(
                    label="分析报告",
                    value="⏳ 等待上传文件..."
                )
        
        analyze_btn.click(
            fn=process_enhanced_report,
            inputs=[docx_input, model_input, max_cases_input],
            outputs=output
        )
        
        gr.Markdown("""
        ---
        ### 💡 使用建议:
        - 首次运行需下载OCR模型，约5-10分钟
        - 建议使用7B以上模型以获得更准确的分析
        - 完整分析时间：10个用例约10-15分钟
        - 报告会标注问题严重程度（高/中/低）
        
        ### 🔧 故障排查:
        ```bash
        # 检查Ollama服务
        ollama serve
        
        # 检查模型列表
        ollama list
        
        # 拉取推荐模型
        ollama pull qwen2.5:7b
        ```
        """)
    
    return demo


if __name__ == "__main__":
    demo = create_ui()
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
        inbrowser=True
    )
