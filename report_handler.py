import mistune
from astrbot.api import logger
from astrbot.core.utils.io import save_temp_img
from PIL import Image
import io
import os

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {
            --blue-50: #eff6ff;
            --blue-100: #dbeafe;
            --blue-200: #bfdbfe;
            --blue-400: #60a5fa;
            --blue-500: #3b82f6;
            --blue-600: #2563eb;
            --blue-700: #1d4ed8;
            --slate-50: #f8fafc;
            --slate-100: #f1f5f9;
            --slate-200: #e2e8f0;
            --slate-700: #334155;
            --slate-800: #1e293b;
            --amber-100: #fef3c7;
            --amber-700: #b45309;
            --pink-600: #db2777;
        }

        body {
            font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background-color: white; /* 统一背景色为白色，防止远程渲染出现边框色差 */
            margin: 0;
            padding: 0;
        }

        .container {
            width: 100%;
            background-color: white;
            padding: 20px;
            box-sizing: border-box;
            zoom: 1.2; /* 适度缩放 */
        }

        .header {
            padding: 16px 24px;
            border-bottom: 1px solid var(--slate-100);
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .header h1 {
            font-size: 1.125rem;
            font-weight: 600;
            color: var(--slate-800);
            margin: 0;
        }

        .calendar-icon {
            color: var(--blue-500);
            width: 20px;
            height: 20px;
        }

        .badge {
            padding: 2px 8px;
            font-size: 0.75rem;
            border-radius: 9999px;
            background-color: var(--amber-100);
            color: var(--amber-700);
            margin-left: 8px;
        }

        .content {
            padding: 24px;
            color: var(--slate-700);
            line-height: 1.6;
            font-size: 16px;
        }

        .prose h1 { font-size: 1.5rem; font-weight: 700; margin-top: 0; margin-bottom: 1rem; color: var(--slate-800); border-bottom: 2px solid var(--blue-100); padding-bottom: 8px; }
        .prose h2 { font-size: 1.25rem; font-weight: 600; margin-top: 1.5rem; margin-bottom: 0.75rem; color: var(--slate-800); }
        .prose h3 { font-size: 1.125rem; font-weight: 500; margin-top: 1rem; margin-bottom: 0.5rem; color: var(--slate-800); }
        .prose p { margin-bottom: 1rem; }
        .prose strong { color: var(--slate-800); font-weight: 600; }
        .prose ul, .prose ol { padding-left: 1.5rem; margin-bottom: 1rem; }
        .prose li { margin-bottom: 0.25rem; }

        .prose blockquote {
            border-left: 4px solid var(--blue-400);
            padding-left: 1rem;
            font-style: italic;
            margin: 1.5rem 0;
            color: var(--slate-700);
            background-color: var(--blue-50);
            padding: 10px 15px;
            border-radius: 0 8px 8px 0;
        }

        .prose table {
            width: 100%;
            border-collapse: collapse;
            margin: 1rem 0;
            border: 1px solid var(--slate-200);
        }

        .prose thead { background-color: var(--blue-50); }
        .prose th { padding: 12px; text-align: left; font-size: 0.875rem; font-weight: 600; color: var(--blue-700); border-bottom: 2px solid var(--blue-200); }
        .prose td { padding: 12px; font-size: 0.875rem; border-bottom: 1px solid var(--slate-100); }

        .prose code {
            background-color: var(--slate-100);
            color: var(--pink-600);
            padding: 2px 4px;
            border-radius: 4px;
            font-size: 0.875rem;
            font-family: monospace;
        }

        .prose pre {
            background-color: var(--slate-50);
            border: 1px solid var(--slate-200);
            padding: 16px;
            border-radius: 8px;
            overflow-x: auto;
            margin: 1rem 0;
        }

        .footer {
            padding: 12px 24px;
            border-top: 1px solid var(--slate-100);
            font-size: 0.75rem;
            color: #94a3b8;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <svg class="calendar-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
            <h1>{{ date }}</h1>
            {% if is_hidden %}
            <span class="badge">已隐藏</span>
            {% endif %}
        </div>
        <div class="content prose">
            {{ report_html | safe }}
        </div>
        <div class="footer">
            Generated by AstrBot StillAlive Plugin
        </div>
    </div>
</body>
</html>
"""


class ReportHandler:
    @staticmethod
    async def render_report(report_data: dict):
        """将报告数据渲染成图片"""
        date = report_data.get("date", "未知日期")
        is_hidden = report_data.get("is_hidden", False)
        markdown_content = report_data.get("markdown", "暂无内容")

        # 使用 mistune 渲染 Markdown
        renderer = mistune.create_markdown(
            plugins=["table", "strikethrough", "task_lists"]
        )
        report_html = renderer(markdown_content)
        
        # 准备完整的 HTML 内容
        tmpl_data = {"date": date, "is_hidden": is_hidden, "report_html": report_html}

        # 检查是否强制使用本地渲染 (用于本地预览测试)
        use_local_debug = os.environ.get("REMOTE_RENDER_DEBUG") == "1"

        if use_local_debug and PLAYWRIGHT_AVAILABLE:
            try:
                logger.info("ReportHandler: 检测到调试模式，正在使用本地 Playwright 渲染预览...")
                final_html = HTML_TEMPLATE.replace("{{ date }}", date) \
                                           .replace("{{ report_html | safe }}", report_html) \
                                           .replace("{% if is_hidden %}", "" if is_hidden else "<!--") \
                                           .replace("{% endif %}", "" if is_hidden else "-->")
                
                async with async_playwright() as p:
                    browser = await p.chromium.launch()
                    context = await browser.new_context(viewport={'width': 600, 'height': 800}, device_scale_factor=2)
                    page = await context.new_page()
                    await page.set_content(final_html)
                    await page.wait_for_load_state("networkidle")
                    image_bytes = await page.screenshot(full_page=True)
                    await browser.close()
                    img = Image.open(io.BytesIO(image_bytes))
                    return save_temp_img(img)
            except Exception as e:
                logger.error(f"ReportHandler: 本地调试渲染失败: {e}")

        # 默认生产逻辑：使用远程 API
        try:
            from astrbot.api import html_renderer
            # 使用官方接口进行渲染
            image_path = await html_renderer.render_custom_template(
                HTML_TEMPLATE, 
                tmpl_data
            )
            return image_path
        except Exception as e:
            logger.error(f"ReportHandler: 远程渲染出错: {e}")
            return None
