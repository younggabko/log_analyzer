import streamlit as st
import re
import json
import zipfile
import tarfile
import io
from pathlib import Path
from collections import deque

st.set_page_config(
    page_title="DUT Log & Vision Image Analysis Dashboard",
    page_icon="🔍",
    layout="wide"
)

# ==========================================
# 1. 압축 파일 및 이미지 추출 헬퍼 함수
# ==========================================
def extract_contents_from_archive(uploaded_file):
    """
    업로드된 파일에서:
    1) 'task_sequence_log.log' 텍스트 라인 추출
    2) vision_image_data_part_#.* 압축파일을 포함한 모든 이미지 파일(.jpg, .jpeg, .png)을
       {파일명: 바이너리_데이터} 딕셔너리로 메모리에 수집
    """
    file_name = uploaded_file.name.lower()
    file_bytes = uploaded_file.read()

    log_lines = None
    log_source_name = None
    image_dict = {}  # { "image_filename.jpg": bytes_data }

    def process_inner_archive(archive_name, inner_bytes):
        """내부 중첩 압축 파일(vision_image_data_part_... 등) 재귀 해제"""
        name_lower = archive_name.lower()
        if name_lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(inner_bytes)) as iz:
                    for f in iz.namelist():
                        f_lower = f.lower()
                        if f_lower.endswith((".jpg", ".jpeg", ".png")):
                            image_dict[Path(f).name] = iz.read(f)
            except Exception:
                pass
        elif name_lower.endswith((".tar", ".tar.gz", ".tgz")):
            try:
                with tarfile.open(fileobj=io.BytesIO(inner_bytes)) as it:
                    for m in it.getmembers():
                        if m.isfile() and m.name.lower().endswith((".jpg", ".jpeg", ".png")):
                            f = it.extractfile(m)
                            if f:
                                image_dict[Path(m.name).name] = f.read()
            except Exception:
                pass

    # 1) ZIP 압축 파일 처리
    if file_name.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                all_files = z.namelist()
                for name in all_files:
                    name_lower = name.lower()
                    
                    # 로그 파일 탐색
                    if Path(name).name.lower() == "task_sequence_log.log":
                        with z.open(name) as lf:
                            log_lines = lf.read().decode('utf-8', errors='ignore').splitlines()
                            log_source_name = name

                    # 중첩 압축 파일 (vision_image_data_part_* 등) 탐색 및 2차 해제
                    elif re.search(r'vision_image_data_part_.*?\.(zip|tar|tar\.gz|tgz)', Path(name).name, re.IGNORECASE):
                        inner_data = z.read(name)
                        process_inner_archive(name, inner_data)

                    # 루트/서브폴더에 바로 풀려있는 이미지 수집
                    elif name_lower.endswith((".jpg", ".jpeg", ".png")):
                        image_dict[Path(name).name] = z.read(name)

        except Exception as e:
            return None, {}, f"ZIP 처리 중 오류: {e}"

    # 2) TAR / TAR.GZ 압축 파일 처리
    elif file_name.endswith((".tar", ".tar.gz", ".tgz")):
        try:
            with tarfile.open(fileobj=io.BytesIO(file_bytes)) as t:
                members = t.getmembers()
                for m in members:
                    if not m.isfile():
                        continue
                    m_lower = m.name.lower()

                    if Path(m.name).name.lower() == "task_sequence_log.log":
                        f = t.extractfile(m)
                        if f:
                            log_lines = f.read().decode('utf-8', errors='ignore').splitlines()
                            log_source_name = m.name

                    elif re.search(r'vision_image_data_part_.*?\.(zip|tar|tar\.gz|tgz)', Path(m.name).name, re.IGNORECASE):
                        f = t.extractfile(m)
                        if f:
                            process_inner_archive(m.name, f.read())

                    elif m_lower.endswith((".jpg", ".jpeg", ".png")):
                        f = t.extractfile(m)
                        if f:
                            image_dict[Path(m.name).name] = f.read()

        except Exception as e:
            return None, {}, f"TAR 처리 중 오류: {e}"

    # 3) 단일 로그 파일 (.log, .txt)
    else:
        log_lines = file_bytes.decode('utf-8', errors='ignore').splitlines()
        log_source_name = uploaded_file.name

    if log_lines is None:
        return None, image_dict, "압축 파일 내에 'task_sequence_log.log' 파일이 발견되지 않았습니다."

    status_msg = f"로그 파일 `{log_source_name}` 파싱 완료 (발견된 이미지: {len(image_dict)}장)"
    return log_lines, image_dict, status_msg


# ==========================================
# 2. 코어 파싱 로직
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

    recent_history_buffer = deque(maxlen=context_line_count)

    in_error_block = False
    current_error_info = {}

    for line_no, raw_line in enumerate(lines, 1):
        line = raw_line.strip()

        # 1. Barcode
        b_match = barcode_pattern.search(line)
        if b_match:
            current_sn = b_match.group(1).strip()
            current_pn = b_match.group(2).strip()

        # 2. OCR Image
        img_match = ocr_image_pattern.search(line)
        if img_match:
            p = Path(img_match.group("full_path"))
            last_ocr_path = str(p.parent) + "/"
            last_ocr_filename = p.name
            recent_ocr_flag = True

        # 3. UX Action
        ux_match = ux_action_pattern.search(line)
        if ux_match:
            last_ux_action = ux_match.group(0).strip()
            recent_ocr_flag = True

        # 4. ERROR Header
        h_match = error_header_pattern.match(line)
        if h_match:
            in_error_block = True
            summary_text = h_match.group("summary")
            is_ocr = bool(recent_ocr_flag or re.search(r'(ocr|display|led|eichrecht|screen|verify_.*_status)', summary_text, re.IGNORECASE))
            
            prior_context = list(recent_history_buffer)

            current_error_info = {
                "line_no": line_no,
                "timestamp": h_match.group("timestamp"),
                "is_ocr_error": is_ocr,
                "category": "OCR 검증 에러" if is_ocr else "일반 DUT 에러",
                "summary": summary_text,
                "sn": current_sn,
                "pn": current_pn,
                "prior_logs": prior_context,
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

        # 5. End of Error Block
        if in_error_block and next_log_pattern.match(line):
            in_error_block = False

        # 6. Inside Error Block
        if in_error_block:
            if line:
                current_error_info["traceback"].append(raw_line.rstrip())
            t_match = target_msg_pattern.search(line)
            if t_match:
                err_msg = t_match.group(0).strip()
                if err_msg not in current_error_info["root_errors"]:
                    current_error_info["root_errors"].append(err_msg)
        else:
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
# 3. 웹 UI 구성 및 이미지 표시
# ==========================================
st.title("🛠️ DUT 테스트 로그 & Vision 이미지 분석 시스템")
st.caption("압축 해제 | task_sequence_log.log 파싱 | vision_image_data_part_#.### 내부 이미지 매핑 및 Display")

uploaded_file = st.file_uploader(
    "로그 및 Vision 이미지 압축파일(.zip, .tar.gz)을 업로드하세요",
    type=["zip", "tar", "gz", "tgz", "log", "txt"]
)

if uploaded_file:
    content_lines, image_cache, status_msg = extract_contents_from_archive(uploaded_file)

    if content_lines is None:
        st.error(status_msg)
    else:
        st.success(f"✅ {status_msg}")

        # 상단 필터 & 슬라이더
        col_filter, col_lines = st.columns([2, 1])
        with col_filter:
            view_mode = st.radio(
                "보고 싶은 로그 유형 선택:",
                ["전체 (All)", "OCR 검증 로그 (OCR Only)", "일반 시퀀스 로그 (General Only)"],
                horizontal=True
            )
        with col_lines:
            context_count = st.slider("에러 직전 로그 라인 수", min_value=10, max_value=100, value=40, step=5)

        mode_map = {
            "전체 (All)": "all",
            "OCR 검증 로그 (OCR Only)": "ocr",
            "일반 시퀀스 로그 (General Only)": "general"
        }
        
        all_results, filtered_results = parse_log_content(
            content_lines, 
            log_type=mode_map[view_mode],
            context_line_count=context_count
        )

        # 통계 메트릭
        ocr_count = sum(1 for r in all_results if r["is_ocr_error"])
        gen_count = len(all_results) - ocr_count
        
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("총 에러 감지", f"{len(all_results)} 건")
        m2.metric("OCR 관련 에러", f"{ocr_count} 건")
        m3.metric("일반 시퀀스 에러", f"{gen_count} 건")
        m4.metric("로드된 Vision 이미지", f"{len(image_cache)} 장")

        st.divider()

        # 에러 목록 렌더링
        if not filtered_results:
            st.info("선택한 필터 조건에 해당하는 에러가 없습니다. (테스트 PASS)")
        else:
            for idx, item in enumerate(filtered_results, 1):
                badge_color = "🔴" if item["is_ocr_error"] else "🔵"
                target_img_name = item["ocr_info"]["image_file"]

                with st.expander(
                    f"{badge_color} [{idx}] Line {item['line_no']} | {item['category']} | SN: {item['sn']} (PN: {item['pn']})", 
                    expanded=(idx == 1)
                ):
                    st.markdown(f"**Task / Error 요약:** `{item['summary']}`")

                    # 핵심 에러 원인 표시
                    if item["root_errors"]:
                        st.error(f"**Root Failure / Error:**\n" + "\n".join([f"- {e}" for e in item["root_errors"]]))

                    # OCR 에러인 경우 이미지 및 검증 데이터 Display
                    if item["is_ocr_error"]:
                        st.markdown("#### 📷 OCR 검증 메타데이터 & 캡처 이미지")
                        col_meta, col_img = st.columns([1, 1])

                        with col_meta:
                            st.info(f"""
                            - **저장 경로**: `{item['ocr_info']['image_path'] or 'N/A'}`
                            - **타겟 파일명**: `{target_img_name or 'N/A'}`
                            - **UX Action**: `{item['ocr_info']['ux_action'] or 'N/A'}`
                            """)

                        with col_img:
                            if target_img_name and target_img_name in image_cache:
                                img_bytes = image_cache[target_img_name]
                                st.image(
                                    img_bytes, 
                                    caption=f"실패 대상 이미지: {target_img_name}", 
                                    use_container_width=True
                                )
                                # 개별 이미지 다운로드 버튼
                                st.download_button(
                                    label="💾 이미지 원본 다운로드",
                                    data=img_bytes,
                                    file_name=target_img_name,
                                    mime="image/jpeg",
                                    key=f"dl_img_{idx}"
                                )
                            elif target_img_name:
                                st.warning(f"⚠️ `{target_img_name}` 파일이 압축파일(`vision_image_data_part_*`) 내에 포함되어 있지 않습니다.")
                            else:
                                st.write("연결된 이미지 파일 정보가 없습니다.")

                    # 탭 분리: 직전 N줄 로그 vs Traceback
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

        # 사이드바 다운로드 옵션
        st.sidebar.header("📥 결과 내보내기")
        json_str = json.dumps(filtered_results, indent=2, ensure_ascii=False)
        st.sidebar.download_button(
            label="분석 결과 JSON 다운로드",
            data=json_str,
            file_name=f"{Path(uploaded_file.name).stem}_analyzed.json",
            mime="application/json"
        )
