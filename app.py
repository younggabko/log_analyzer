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
    file_name = uploaded_file.name.lower()
    file_bytes = uploaded_file.read()

    log_lines = None
    log_source_name = None
    image_dict = {}

    def process_inner_archive(archive_name, inner_bytes):
        name_lower = archive_name.lower()
        if name_lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(inner_bytes)) as iz:
                    for f in iz.namelist():
                        if f.lower().endswith((".jpg", ".jpeg", ".png")):
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

    # 1) ZIP 처리
    if file_name.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                for name in z.namelist():
                    name_lower = name.lower()
                    if Path(name).name.lower() == "task_sequence_log.log":
                        with z.open(name) as lf:
                            log_lines = lf.read().decode('utf-8', errors='ignore').splitlines()
                            log_source_name = name
                    elif re.search(r'vision_image_data_part_.*?\.(zip|tar|tar\.gz|tgz)', Path(name).name, re.IGNORECASE):
                        inner_data = z.read(name)
                        process_inner_archive(name, inner_data)
                    elif name_lower.endswith((".jpg", ".jpeg", ".png")):
                        image_dict[Path(name).name] = z.read(name)
        except Exception as e:
            return None, {}, f"ZIP 처리 중 오류: {e}"

    # 2) TAR / TAR.GZ 처리
    elif file_name.endswith((".tar", ".tar.gz", ".tgz")):
        try:
            with tarfile.open(fileobj=io.BytesIO(file_bytes)) as t:
                for m in t.getmembers():
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

    # 3) 단일 로그 파일
    else:
        log_lines = file_bytes.decode('utf-8', errors='ignore').splitlines()
        log_source_name = uploaded_file.name

    if log_lines is None:
        return None, image_dict, "압축 파일 내에 'task_sequence_log.log' 파일이 발견되지 않았습니다."

    status_msg = f"로그 파일 `{log_source_name}` 파싱 완료 (발견된 이미지: {len(image_dict)}장)"
    return log_lines, image_dict, status_msg


# ==========================================
# 2. 코어 파싱 로직 (중요 연계 이미지 최대 2장 보관)
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
    ftp_download_pattern = re.compile(r"FTP server\s+['\"][^'\"]+['\"]\s+file download\s+['\"](?P<full_path>.*?/(?P<filename>[^/'\"]+))['\"]\s+completed\.", re.IGNORECASE)

    results = []
    current_sn = "N/A"
    current_pn = "N/A"
    last_ux_action = None
    recent_ocr_flag = False

    # 에러 직전 발생한 최근 이미지 최대 2개만 유지하는 큐
    recent_images_queue = deque(maxlen=2)
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

        # 2. OCR Image 패턴
        img_match = ocr_image_pattern.search(line)
        if img_match:
            p = Path(img_match.group("full_path"))
            # 중복 등록 방지 후 큐 추가 (최대 2개 유지)
            if not any(item["filename"] == p.name for item in recent_images_queue):
                recent_images_queue.append({
                    "path": str(p.parent) + "/",
                    "filename": p.name
                })
            recent_ocr_flag = True

        # 3. FTP Download 패턴
        ftp_match = ftp_download_pattern.search(line)
        if ftp_match:
            p = Path(ftp_match.group("full_path"))
            if not any(item["filename"] == p.name for item in recent_images_queue):
                recent_images_queue.append({
                    "path": str(p.parent) + "/",
                    "filename": p.name
                })
            recent_ocr_flag = True

        # 4. UX Action 추적
        ux_match = ux_action_pattern.search(line)
        if ux_match:
            last_ux_action = ux_match.group(0).strip()
            recent_ocr_flag = True

        # 5. ERROR/EER 헤더 감지
        h_match = error_header_pattern.match(line)
        if h_match:
            in_error_block = True
            summary_text = h_match.group("summary")
            is_ocr = bool(recent_ocr_flag or len(recent_images_queue) > 0 or re.search(r'(ocr|vision|display|led|eichrecht|screen|verify_.*_status)', summary_text, re.IGNORECASE))
            
            prior_context = list(recent_history_buffer)

            # 직전 중요 이미지 최대 2개 복사 (최신 이미지가 뒤에 위치)
            captured_images = list(recent_images_queue)

            current_error_info = {
                "line_no": line_no,
                "timestamp": h_match.group("timestamp"),
                "is_ocr_error": is_ocr,
                "category": "Vision/OCR 검증 에러" if is_ocr else "일반 DUT 에러",
                "summary": summary_text,
                "sn": current_sn,
                "pn": current_pn,
                "prior_logs": prior_context,
                "linked_images": captured_images,  # 최대 2개 이미지
                "ux_action": last_ux_action if is_ocr else None,
                "traceback": [],
                "root_errors": []
            }
            results.append(current_error_info)

            header_err = target_msg_pattern.search(summary_text)
            if header_err:
                current_error_info["root_errors"].append(header_err.group(0).strip())

            recent_ocr_flag = False
            continue

        # 6. 에러 블록 종료
        if in_error_block and next_log_pattern.match(line):
            in_error_block = False

        # 7. 에러 블록 내부 내용 수집
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
# 3. 웹 UI 구성 및 연계 이미지 (최대 2개) Display
# ==========================================
st.title("🛠️ DUT 테스트 로그 & Vision 이미지 분석 시스템")
st.caption("에러 연계 주요 이미지(최대 2장) 비교 | task_sequence_log.log 파싱 | 직전 Context 40줄")

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

        col_filter, col_lines = st.columns([2, 1])
        with col_filter:
            view_mode = st.radio(
                "보고 싶은 로그 유형 선택:",
                ["전체 (All)", "Vision/OCR 검증 로그 (OCR Only)", "일반 시퀀스 로그 (General Only)"],
                horizontal=True
            )
        with col_lines:
            context_count = st.slider("에러 직전 로그 라인 수", min_value=10, max_value=100, value=40, step=5)

        mode_map = {
            "전체 (All)": "all",
            "Vision/OCR 검증 로그 (OCR Only)": "ocr",
            "일반 시퀀스 로그 (General Only)": "general"
        }
        
        all_results, filtered_results = parse_log_content(
            content_lines, 
            log_type=mode_map[view_mode],
            context_line_count=context_count
        )

        ocr_count = sum(1 for r in all_results if r["is_ocr_error"])
        gen_count = len(all_results) - ocr_count
        
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("총 에러 감지", f"{len(all_results)} 건")
        m2.metric("Vision/OCR 에러", f"{ocr_count} 건")
        m3.metric("일반 시퀀스 에러", f"{gen_count} 건")
        m4.metric("로드된 Vision 이미지", f"{len(image_cache)} 장")

        st.divider()

        if not filtered_results:
            st.info("선택한 필터 조건에 해당하는 에러가 없습니다. (테스트 PASS)")
        else:
            for idx, item in enumerate(filtered_results, 1):
                badge_color = "🔴" if item["is_ocr_error"] else "🔵"
                linked_imgs = item["linked_images"]  # 최대 2개

                with st.expander(
                    f"{badge_color} [{idx}] Line {item['line_no']} | {item['category']} | SN: {item['sn']} (PN: {item['pn']})", 
                    expanded=(idx == 1)
                ):
                    st.markdown(f"**Task / Error 요약:** `{item['summary']}`")

                    # 핵심 에러 원인
                    if item["root_errors"]:
                        st.error(f"**Root Failure / Error:**\n" + "\n".join([f"- {e}" for e in item["root_errors"]]))

                    # Vision / OCR 에러 시 연계 이미지 최대 2장 Display
                    if item["is_ocr_error"]:
                        st.markdown("#### 📷 Vision / OCR 연계 이미지 (최대 2장)")
                        
                        if item["ux_action"]:
                            st.info(f"**UX Action Verify:** `{item['ux_action']}`")

                        if linked_imgs:
                            # 1장이면 col 1개, 2장이면 col 2개 균등 분할
                            cols = st.columns(len(linked_imgs))
                            for c_idx, img_info in enumerate(linked_imgs):
                                with cols[c_idx]:
                                    tag = "최근 1순위 (실패 직전)" if c_idx == len(linked_imgs) - 1 else "최근 2순위 (이전 스텝)"
                                    st.markdown(f"**[{tag}]**")
                                    st.caption(f"파일명: `{img_info['filename']}`")
                                    st.caption(f"경로: `{img_info['path']}`")

                                    fname = img_info['filename']
                                    if fname in image_cache:
                                        img_bytes = image_cache[fname]
                                        st.image(
                                            img_bytes, 
                                            caption=fname, 
                                            use_container_width=True
                                        )
                                        st.download_button(
                                            label=f"💾 {fname} 다운로드",
                                            data=img_bytes,
                                            file_name=fname,
                                            mime="image/jpeg",
                                            key=f"dl_{idx}_{c_idx}"
                                        )
                                    else:
                                        st.warning(f"⚠️ 압축 내 `{fname}` 바이너리 파일 없음")
                        else:
                            st.write("연계된 이미지 파일 정보가 없습니다.")

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
