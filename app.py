import streamlit as st
import re
import json
from pathlib import Path
from collections import deque

st.set_page_config(
    page_title="DUT Log Analysis Dashboard",
    page_icon="🔍",
    layout="wide"
)

# ==========================================
# 코어 파싱 로직 (직전 40줄 버퍼 포함)
# ==========================================
def parse_log_content(lines, log_type="all", context_line_count=40):
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

    # 에러 직전 N줄을 보관하는 링 버퍼 (최대 40줄 유지)
    recent_history_buffer = deque(maxlen=context_line_count)

    in_error_block = False
    current_error_info = {}

    for line_no, raw_line in enumerate(lines, 1):
        line = raw_line.strip()

        # 1. Barcode 추적
        b_match = barcode_pattern.search(line)
        if b_match:
            current_sn = b_match.group(1).strip()
            current_pn = b_match.group(2).strip()

        # 2. OCR Image 추적
        img_match = ocr_image_pattern.search(line)
        if img_match:
            p = Path(img_match.group("full_path"))
            last_ocr_path = str(p.parent) + "/"
            last_ocr_filename = p.name
            recent_ocr_flag = True

        # 3. UX Action 추적
        ux_match = ux_action_pattern.search(line)
        if ux_match:
            last_ux_action = ux_match.group(0).strip()
            recent_ocr_flag = True

        # 4. ERROR/EER 헤더 감지
        h_match = error_header_pattern.match(line)
        if h_match:
            in_error_block = True
            summary_text = h_match.group("summary")
            is_ocr = bool(recent_ocr_flag or re.search(r'(ocr|display|led|eichrecht|screen|verify_.*_status)', summary_text, re.IGNORECASE))
            
            # 에러 감지 시점 기준 직전 40줄 스냅샷 복사
            prior_context = list(recent_history_buffer)

            current_error_info = {
                "line_no": line_no,
                "timestamp": h_match.group("timestamp"),
                "is_ocr_error": is_ocr,
                "category": "OCR 검증 에러" if is_ocr else "일반 DUT 에러",
                "summary": summary_text,
                "sn": current_sn,
                "pn": current_pn,
                "prior_logs": prior_context,  # 에러 발생 이전 40라인 로그
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

        # 5. 다음 일반 로그 시작 시 에러 블록 종료
        if in_error_block and next_log_pattern.match(line):
            in_error_block = False

        # 6. 에러 블록 내부 (Traceback & 예외 메시지 수집)
        if in_error_block:
            if line:
                current_error_info["traceback"].append(raw_line.rstrip())
            t_match = target_msg_pattern.search(line)
            if t_match:
                err_msg = t_match.group(0).strip()
                if err_msg not in current_error_info["root_errors"]:
                    current_error_info["root_errors"].append(err_msg)
        else:
            # 에러 블록 밖의 일반 로그만 히스토리 버퍼에 기록
            if line:
                recent_history_buffer.append(f"L{line_no:05d}: {raw_line.rstrip()}")

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
st.caption("ScanBarcode(SN, PN), 직전 40줄 Context 로그, OCR 이미지 정보, UX Action, Traceback 자동 분석")

uploaded_file = st.file_uploader("분석할 로그 파일(.log, .txt)을 드래그하거나 선택하세요", type=["log", "txt"])

if uploaded_file:
    content = uploaded_file.read().decode('utf-8', errors='ignore').splitlines()
    
    # 상단 옵션 바
    col_filter, col_lines = st.columns([2, 1])
    with col_filter:
        view_mode = st.radio(
            "보고 싶은 로그 유형 선택:",
            ["전체 (All)", "OCR 검증 로그 (OCR Only)", "일반 시퀀스 로그 (General Only)"],
            horizontal=True
        )
    with col_lines:
        context_count = st.slider("에러 이전 포함할 로그 라인 수", min_value=10, max_value=100, value=40, step=5)

    mode_map = {
        "전체 (All)": "all",
        "OCR 검증 로그 (OCR Only)": "ocr",
        "일반 시퀀스 로그 (General Only)": "general"
    }
    
    all_results, filtered_results = parse_log_content(
        content, 
        log_type=mode_map[view_mode],
        context_line_count=context_count
    )

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
                
                # OCR 메타데이터가 있을 때 표시
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

                # 탭 형태로 직전 40줄 로그와 Traceback을 깔끔하게 분리
                tab1, tab2 = st.tabs([f"📋 에러 직전 로그 ({len(item['prior_logs'])} 라인)", "🔍 Traceback 전체 스택"])
                
                with tab1:
                    if item["prior_logs"]:
                        st.code("\n".join(item["prior_logs"]), language="text")
                    else:
                        st.write("표시할 이전 로그가 없습니다.")

                with tab2:
                    if item["traceback"]:
                        st.code("\n".join(item["traceback"]), language="python")
                    else:
                        st.write("Traceback 로그가 없습니다.")

    # 결과 다운로드 버튼
    st.sidebar.header("📥 결과 내보내기")
    json_str = json.dumps(filtered_results, indent=2, ensure_ascii=False)
    st.sidebar.download_button(
        label="분석 결과 JSON 다운로드",
        data=json_str,
        file_name=f"{uploaded_file.name}_analyzed.json",
        mime="application/json"
    )
