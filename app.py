import streamlit as st
import re
import json
from pathlib import Path

st.set_page_config(
    page_title="DUT Log Analysis Dashboard",
    page_icon="🔍",
    layout="wide"
)

# ==========================================
# 코어 파싱 로직
# ==========================================
def parse_log_content(lines, log_type="all"):
    barcode_pattern = re.compile(r'ScanBarcode\s*:\s*([^,\s]+)\s*,\s*([^\s,]+)', re.IGNORECASE)
    error_header_pattern = re.compile(
        r'^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+'
        r'(?:ERROR|ERR|EER)\[\d+\]:\s*(?P<summary>.*)'
    )
    next_log_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\s+[A-Z]+\[\d+\]:')
    target_msg_pattern = re.compile(r'(?:[A-Za-z0-9_.]*(?:Failure|Error))\s*:\s*(.*)', re.IGNORECASE)
    ux_action_pattern = re.compile(r'UX Action Verify:(.*)')
    ocr_image_pattern = re.compile(r'OCR image\[(?P<full_path>.*?/(?P<filename>[^/]+))\]\s+save complete\.', re.IGNORECASE)

    results = []
    current_sn = "N/A"
    current_pn = "N/A"
    last_ux_action = None
    last_ocr_path = None
    last_ocr_filename = None
    recent_ocr_flag = False

    in_error_block = False
    current_error_info = {}

    for line_no, raw_line in enumerate(lines, 1):
        line = raw_line.strip()

        # Barcode
        b_match = barcode_pattern.search(line)
        if b_match:
            current_sn = b_match.group(1).strip()
            current_pn = b_match.group(2).strip()

        # OCR Image
        img_match = ocr_image_pattern.search(line)
        if img_match:
            p = Path(img_match.group("full_path"))
            last_ocr_path = str(p.parent) + "/"
            last_ocr_filename = p.name
            recent_ocr_flag = True

        # UX Action
        ux_match = ux_action_pattern.search(line)
        if ux_match:
            last_ux_action = ux_match.group(0).strip()
            recent_ocr_flag = True

        # Error Header
        h_match = error_header_pattern.match(line)
        if h_match:
            in_error_block = True
            summary_text = h_match.group("summary")
            is_ocr = bool(recent_ocr_flag or re.search(r'(ocr|display|led|eichrecht|screen|verify_.*_status)', summary_text, re.IGNORECASE))
            
            current_error_info = {
                "line_no": line_no,
                "timestamp": h_match.group("timestamp"),
                "is_ocr_error": is_ocr,
                "category": "OCR 검증 에러" if is_ocr else "일반 DUT 에러",
                "summary": summary_text,
                "sn": current_sn,
                "pn": current_pn,
                "ocr_info": {
                    "image_path": last_ocr_path if is_ocr else None,
                    "image_file": last_ocr_filename if is_ocr else None,
                    "ux_action": last_ux_action if is_ocr else None
                },
                "traceback": [],
                "root_errors": []
            }
            results.append(current_error_info)

            header_err = target_msg_pattern.search(summary_text)
            if header_err:
                current_error_info["root_errors"].append(header_err.group(0).strip())

            recent_ocr_flag = False
            continue

        if in_error_block and next_log_pattern.match(line):
            in_error_block = False

        if in_error_block:
            if line:
                current_error_info["traceback"].append(raw_line.rstrip())
            t_match = target_msg_pattern.search(line)
            if t_match:
                err_msg = t_match.group(0).strip()
                if err_msg not in current_error_info["root_errors"]:
                    current_error_info["root_errors"].append(err_msg)

    # 필터링
    if log_type == "ocr":
        filtered = [r for r in results if r["is_ocr_error"]]
    elif log_type == "general":
        filtered = [r for r in results if not r["is_ocr_error"]]
    else:
        filtered = results

    return results, filtered


# ==========================================
# 웹 UI 화면 구성
# ==========================================
st.title("🛠️ DUT 테스트 로그 자동 분석 시스템")
st.caption("ScanBarcode(SN, PN), OCR 이미지 정보, UX Action, Traceback 자동 분류")

uploaded_file = st.file_uploader("분석할 로그 파일(.log, .txt)을 드래그하거나 선택하세요", type=["log", "txt"])

if uploaded_file:
    # 파일 디코딩
    content = uploaded_file.read().decode('utf-8', errors='ignore').splitlines()
    
    # 상단 컨트롤 바
    col_filter, col_stat = st.columns([1, 2])
    with col_filter:
        view_mode = st.radio(
            "보고 싶은 로그 유형 선택:",
            ["전체 (All)", "OCR 검증 로그 (OCR Only)", "일반 시퀀스 로그 (General Only)"],
            horizontal=True
        )

    mode_map = {
        "전체 (All)": "all",
        "OCR 검증 로그 (OCR Only)": "ocr",
        "일반 시퀀스 로그 (General Only)": "general"
    }
    
    all_results, filtered_results = parse_log_content(content, log_type=mode_map[view_mode])

    # 통계 메트릭 표시
    ocr_count = sum(1 for r in all_results if r["is_ocr_error"])
    gen_count = len(all_results) - ocr_count
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("총 에러 감지", f"{len(all_results)} 건")
    m2.metric("OCR 관련 에러", f"{ocr_count} 건")
    m3.metric("일반 시퀀스 에러", f"{gen_count} 건")
    m4.metric("현재 화면 표시", f"{len(filtered_results)} 건")

    st.divider()

    # 에러 목록 렌더링
    if not filtered_results:
        st.success("선택한 필터 조건에 해당하는 에러가 없습니다.")
    else:
        for idx, item in enumerate(filtered_results, 1):
            badge_color = "🔴" if item["is_ocr_error"] else "🔵"
            with st.expander(f"{badge_color} [{idx}] Line {item['line_no']} | {item['category']} | SN: {item['sn']} (PN: {item['pn']})", expanded=(idx == 1)):
                st.markdown(f"**Task / Error 요약:** `{item['summary']}`")
                
                # OCR 정보가 있을 때 전용 섹션 표시
                if item["is_ocr_error"] and any(item["ocr_info"].values()):
                    st.info(f"""
                    **📷 연관 OCR 검증 데이터**
                    - **저장 경로**: `{item['ocr_info']['image_path'] or 'N/A'}`
                    - **이미지 파일명**: `{item['ocr_info']['image_file'] or 'N/A'}`
                    - **UX Action**: `{item['ocr_info']['ux_action'] or 'N/A'}`
                    """)

                # 핵심 에러 원인
                if item["root_errors"]:
                    st.error(f"**Root Failure / Error:**\n" + "\n".join([f"- {e}" for e in item["root_errors"]]))

                # Traceback 풀 스택
                if item["traceback"]:
                    st.markdown("**Full Traceback:**")
                    st.code("\n".join(item["traceback"]), language="python")

    # 결과 다운로드 버튼
    st.sidebar.header("📥 결과 내보내기")
    json_str = json.dumps(filtered_results, indent=2, ensure_ascii=False)
    st.sidebar.download_button(
        label="분석 결과 JSON 다운로드",
        data=json_str,
        file_name=f"{uploaded_file.name}_analyzed.json",
        mime="application/json"
    )
