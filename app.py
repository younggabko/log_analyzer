import streamlit as st
import re
import json
import zipfile
import tarfile
import io
import pandas as pd
from pathlib import Path
from collections import deque

st.set_page_config(
    page_title="DUT IV2 Vision & Display Log Analyzer",
    page_icon="🔬",
    layout="wide"
)

# ==========================================
# 1. 압축 파일 내 로그, 이미지, SequenceReport JSON 수집
# ==========================================
def extract_contents_from_archive(uploaded_file):
    file_name = uploaded_file.name.lower()
    file_bytes = uploaded_file.read()

    log_lines = None
    log_source_name = None
    image_dict = {}
    sequence_report_data = None
    sequence_report_filename = None

    # SequenceReport_###_####-###.json 및 변형 패턴 매칭 지원
    sequence_json_pattern = re.compile(r'^SequenceReport_.*\.json$', re.IGNORECASE)

    def process_inner_archive(archive_name, inner_bytes):
        name_lower = archive_name.lower()
        if name_lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(inner_bytes)) as iz:
                    for f in iz.namelist():
                        if f.lower().endswith((".jpg", ".jpeg", ".png", ".svg")):
                            image_dict[Path(f).name] = iz.read(f)
            except Exception:
                pass
        elif name_lower.endswith((".tar", ".tar.gz", ".tgz")):
            try:
                with tarfile.open(fileobj=io.BytesIO(inner_bytes)) as it:
                    for m in it.getmembers():
                        if m.isfile() and m.name.lower().endswith((".jpg", ".jpeg", ".png", ".svg")):
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
                    base_name = Path(name).name
                    if base_name.lower() == "task_sequence_log.log":
                        with z.open(name) as lf:
                            log_lines = lf.read().decode('utf-8', errors='ignore').splitlines()
                            log_source_name = name
                    elif sequence_json_pattern.match(base_name):
                        with z.open(name) as jf:
                            try:
                                sequence_report_data = json.loads(jf.read().decode('utf-8', errors='ignore'))
                                sequence_report_filename = base_name
                            except Exception:
                                pass
                    elif re.search(r'vision_image_data_part_.*?\.(zip|tar|tar\.gz|tgz)', base_name, re.IGNORECASE):
                        inner_data = z.read(name)
                        process_inner_archive(name, inner_data)
                    elif base_name.lower().endswith((".jpg", ".jpeg", ".png", ".svg")):
                        image_dict[base_name] = z.read(name)
        except Exception as e:
            return None, {}, None, None, f"ZIP 처리 오류: {e}"

    # 2) TAR / TAR.GZ 처리
    elif file_name.endswith((".tar", ".tar.gz", ".tgz")):
        try:
            with tarfile.open(fileobj=io.BytesIO(file_bytes)) as t:
                for m in t.getmembers():
                    if not m.isfile():
                        continue
                    base_name = Path(m.name).name
                    if base_name.lower() == "task_sequence_log.log":
                        f = t.extractfile(m)
                        if f:
                            log_lines = f.read().decode('utf-8', errors='ignore').splitlines()
                            log_source_name = m.name
                    elif sequence_json_pattern.match(base_name):
                        f = t.extractfile(m)
                        if f:
                            try:
                                sequence_report_data = json.loads(f.read().decode('utf-8', errors='ignore'))
                                sequence_report_filename = base_name
                            except Exception:
                                pass
                    elif re.search(r'vision_image_data_part_.*?\.(zip|tar|tar\.gz|tgz)', base_name, re.IGNORECASE):
                        f = t.extractfile(m)
                        if f:
                            process_inner_archive(m.name, f.read())
                    elif base_name.lower().endswith((".jpg", ".jpeg", ".png", ".svg")):
                        f = t.extractfile(m)
                        if f:
                            image_dict[base_name] = f.read()
        except Exception as e:
            return None, {}, None, None, f"TAR 처리 오류: {e}"

    # 3) 단일 파일
    else:
        if sequence_json_pattern.match(Path(uploaded_file.name).name):
            try:
                sequence_report_data = json.loads(file_bytes.decode('utf-8', errors='ignore'))
                sequence_report_filename = uploaded_file.name
            except Exception:
                pass
        else:
            log_lines = file_bytes.decode('utf-8', errors='ignore').splitlines()
            log_source_name = uploaded_file.name

    if log_lines is None and sequence_report_data is None:
        return None, image_dict, None, None, "압축 파일 내에 분석 대상 로그 및 SequenceReport 파일이 없습니다."

    status_msg = f"로드 성공: 로그({log_source_name or 'N/A'}), 이미지({len(image_dict)}개), 시퀀스 리포트({sequence_report_filename or '없음'})"
    return log_lines, image_dict, sequence_report_data, sequence_report_filename, status_msg


# ==========================================
# 2. 코어 파싱 로직 (중복 취합 방지 및 정밀 분류)
# ==========================================
def parse_log_content(lines, image_keys, log_type="all", context_line_count=40):
    if not lines:
        return [], []

    barcode_pattern = re.compile(r'ScanBarcode\s*:\s*([^,\s]+)\s*,\s*([^\s,]+)', re.IGNORECASE)
    task_start_pattern = re.compile(r'Executing Task:\s*([A-Za-z0-9_]+)', re.IGNORECASE)
    task_in_error_pattern = re.compile(r'task\s+([A-Za-z0-9_\-]+)\s+failed', re.IGNORECASE)

    error_header_pattern = re.compile(
        r'^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+'
        r'(?:ERROR|ERR|EER)\[\d+\]:\s*(?P<summary>.*)'
    )
    next_log_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\s+[A-Z]+\[\d+\]:')
    target_msg_pattern = re.compile(r'(?:[A-Za-z0-9_.]*(?:Failure|Error))\s*:\s*(.*)', re.IGNORECASE)

    measurement_pattern = re.compile(r'(?:String Measurement with a measured value|Measurement):\s*(.*)', re.IGNORECASE)
    led_state_pattern = re.compile(r'Running LEDs state check:\s*([^\s]+)', re.IGNORECASE)
    ux_action_pattern = re.compile(r'UX Action Verify:(.*)')

    ocr_image_pattern = re.compile(r'OCR image\[(?P<full_path>.*?/(?P<filename>[^/]+))\]\s+save complete\.', re.IGNORECASE)
    ftp_download_pattern = re.compile(r"FTP server\s+['\"][^'\"]+['\"]\s+file download\s+['\"](?P<full_path>.*?/(?P<filename>[^/'\"]+))['\"]\s+completed\.", re.IGNORECASE)

    results = []
    current_sn = "N/A"
    current_pn = "N/A"
    current_task = "Unknown"

    last_measurement = None
    last_led_state = None
    last_ux_action = None
    recent_images_queue = deque(maxlen=2)
    recent_history_buffer = deque(maxlen=context_line_count)

    in_error_block = False
    current_error_info = {}

    GENERAL_TASK_KEYWORDS = ['leakage', 'relay', 'load', 'power', 'ground', 'interrup', 'trip_time', 'voltage', 'current', 'ccid']

    for line_no, raw_line in enumerate(lines, 1):
        line = raw_line.strip()

        # Barcode 추적
        b_match = barcode_pattern.search(line)
        if b_match:
            current_sn = b_match.group(1).strip()
            current_pn = b_match.group(2).strip()

        # Task 시작 감지
        t_match = task_start_pattern.search(line)
        if t_match:
            current_task = t_match.group(1).strip()
            last_measurement = None
            last_led_state = None
            last_ux_action = None
            recent_images_queue.clear()

        # 계측치 및 상태 추적
        m_match = measurement_pattern.search(line)
        if m_match:
            last_measurement = m_match.group(1).strip()

        led_match = led_state_pattern.search(line)
        if led_match:
            last_led_state = led_match.group(1).strip()

        ux_match = ux_action_pattern.search(line)
        if ux_match:
            last_ux_action = ux_match.group(0).strip()

        # 이미지 패턴 감지
        ocr_match = ocr_image_pattern.search(line)
        if ocr_match:
            p = Path(ocr_match.group("full_path"))
            if not any(item["filename"] == p.name for item in recent_images_queue):
                recent_images_queue.append({"path": str(p.parent) + "/", "filename": p.name, "source": "OCR"})

        ftp_match = ftp_download_pattern.search(line)
        if ftp_match:
            p = Path(ftp_match.group("full_path"))
            if not any(item["filename"] == p.name for item in recent_images_queue):
                recent_images_queue.append({"path": str(p.parent) + "/", "filename": p.name, "source": "FTP"})

        # ERROR 감지
        h_match = error_header_pattern.match(line)
        if h_match:
            summary_text = h_match.group("summary")

            detected_task = current_task
            t_err_match = task_in_error_pattern.search(summary_text)
            if t_err_match:
                detected_task = t_err_match.group(1).strip()

            # 중복 에러 병합 (Deduplication)
            if results:
                prev_err = results[-1]
                line_diff = line_no - prev_err["line_no"]
                is_same_task = (prev_err["task_name"] != "Unknown" and prev_err["task_name"] == detected_task)

                if is_same_task and line_diff < 150:
                    if summary_text not in prev_err["summary"]:
                        prev_err["summary"] += f" | {summary_text}"
                    in_error_block = True
                    current_error_info = prev_err
                    continue

            in_error_block = True

            is_explicit_general = any(k in detected_task.lower() for k in GENERAL_TASK_KEYWORDS) or \
                                  any(k in summary_text.lower() for k in GENERAL_TASK_KEYWORDS)

            if is_explicit_general:
                is_iv2 = False
                is_ocr = False
            else:
                is_iv2 = bool(
                    re.search(r'(_led|_display|led_|display_|backlight|lvds|brightness|color)', summary_text, re.IGNORECASE) or
                    re.search(r'(_led|_display|_backlight|led_|display_|backlight_)', detected_task, re.IGNORECASE)# or last_measurement is not None
                )
                is_ocr = bool(is_iv2 or len(recent_images_queue) > 0 or last_ux_action or
                              re.search(r'(_ocr|_vision|ocr_|vision_|eichrecht|verify_.*_status)', summary_text, re.IGNORECASE))
            prior_context = list(recent_history_buffer)
            captured_images = list(recent_images_queue) if not is_explicit_general else []

            # IV2 스마트 이미지 매칭
            if is_iv2 and not is_explicit_general and len(captured_images) < 2:
                search_keywords = []
                for kw in ['red', 'blue', 'green', 'white', 'amber', 'display', 'led']:
                    if kw in summary_text.lower() or kw in detected_task.lower() or (last_led_state and kw in last_led_state.lower()):
                        search_keywords.append(kw)

                graph_candidates = [
                    f for f in image_keys 
                    if f.lower().startswith("verify_") and "_graph" in f.lower() and (not search_keywords or any(k in f.lower() for k in search_keywords))
                ]
                pass_fail_candidates = [
                    f for f in image_keys 
                    if f.lower().startswith("verify_") and any(ext in f.lower() for ext in ["_pass.", "_fail."]) and (not search_keywords or any(k in f.lower() for k in search_keywords))
                ]

                for g_file in graph_candidates:
                    if not any(item["filename"] == g_file for item in captured_images):
                        captured_images.append({"path": "(IV2 Graph Auto-Matched)", "filename": g_file, "source": "IV2 Graph"})
                        break

                for p_file in pass_fail_candidates:
                    if not any(item["filename"] == p_file for item in captured_images):
                        captured_images.append({"path": "(IV2 Result Auto-Matched)", "filename": p_file, "source": "IV2 Result"})
                        break

            if is_iv2:
                category_label = "IV2 Vision (LED/Display) 에러"
            elif is_ocr:
                category_label = "Vision/OCR 검증 에러"
            else:
                category_label = "일반 DUT 에러"

            current_error_info = {
                "line_no": line_no,
                "timestamp": h_match.group("timestamp"),
                "task_name": detected_task,
                "is_ocr_error": is_ocr,
                "is_iv2_error": is_iv2,
                "category": category_label,
                "summary": summary_text,
                "sn": current_sn,
                "pn": current_pn,
                "measurement": last_measurement if not is_explicit_general else None,
                "led_state": last_led_state if not is_explicit_general else None,
                "prior_logs": prior_context,
                "linked_images": captured_images[-2:] if len(captured_images) > 2 else captured_images,
                "ux_action": last_ux_action if is_ocr else None,
                "traceback": [],
                "root_errors": []
            }
            results.append(current_error_info)

            header_err = target_msg_pattern.search(summary_text)
            if header_err:
                current_error_info["root_errors"].append(header_err.group(0).strip())

            last_measurement = None
            last_ux_action = None
            recent_images_queue.clear()
            continue

        # 에러 블록 종료
        if in_error_block and next_log_pattern.match(line):
            in_error_block = False

        # 내용 수집
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

    if log_type == "ocr":
        filtered = [r for r in results if r["is_ocr_error"]]
    elif log_type == "general":
        filtered = [r for r in results if not r["is_ocr_error"]]
    else:
        filtered = results

    return results, filtered


# ==========================================
# 3. Streamlit 대시보드 UI
# ==========================================
st.title("🔬 DUT IV2 Vision & Display 로그 분석 시스템")
st.caption("SequenceReport(.json) 전체 상세 뷰어 | task_results JSON 모드 | 중복 취합 제거 | 직전 40줄 Context")

uploaded_file = st.file_uploader(
    "DUT 로그, SequenceReport JSON 또는 압축파일(.zip, .tar.gz)을 업로드하세요",
    type=["zip", "tar", "gz", "tgz", "log", "txt", "json"]
)

if uploaded_file:
    content_lines, image_cache, seq_report, seq_filename, status_msg = extract_contents_from_archive(uploaded_file)

    if content_lines is None and seq_report is None:
        st.error(status_msg)
    else:
        st.success(f"✅ {status_msg}")

        # -------------------------------------------------------------
        # SequenceReport 렌더링 섹션 (JSON 상세 모드 포함)
        # -------------------------------------------------------------
        if seq_report:
            col_rep_btn, col_rep_info = st.columns([1, 3])
            with col_rep_btn:
                show_report = st.toggle("📊 Sequence Report 보기", value=False)
            with col_rep_info:
                st.caption(f"📁 감지된 리포트: `{seq_filename}`")

            if show_report:
                st.markdown("### 📋 Sequence Report 상세 결과")
                summary = seq_report.get("summary", {})
                task_results = seq_report.get("task_results", [])

                # 1. Summary 메타데이터 카드 표시
                status_color = "🔴" if summary.get("status") == "Failed" else "🟢"
                st.markdown(f"#### {status_color} Test Summary (`Status: {summary.get('status', 'Unknown')}`)")

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Serial Number", summary.get("serial_number", "N/A"))
                c2.metric("Part Number", summary.get("part_number", "N/A"))
                c3.metric("Operation", summary.get("operation", "N/A"))
                duration_sec = round(summary.get("duration_ms", 0) / 1000, 2)
                c4.metric("Test Duration", f"{duration_sec}s")

                with st.expander("ℹ️ Summary 상세 정보 JSON 전체 보기"):
                    st.json(summary)

                # 2. Task Results 표시 모드 선택
                st.markdown("#### ⚙️ Task Results 실행 내역")

                col_view_type, _ = st.columns([1, 2])
                with col_view_type:
                    task_view_mode = st.radio(
                        "Task Results 표시 형식 선택:",
                        ["📊 요약 테이블 + 개별 상세", "📑 전체 원본 JSON 모드"],
                        horizontal=True
                    )

                if task_results:
                    if task_view_mode == "📊 요약 테이블 + 개별 상세":
                        # 요약 테이블
                        table_rows = []
                        for idx, t in enumerate(task_results, 1):
                            t_status = t.get("status", "N/A")
                            t_icon = "❌ Failed" if t_status == "Failed" else ("✅ Passed" if t_status == "Passed" else t_status)
                            dur_s = round(t.get("duration_ms", 0) / 1000, 3)
                            table_rows.append({
                                "No": idx,
                                "Task Name": t.get("name"),
                                "Status": t_icon,
                                "Duration (s)": dur_s,
                                "Message": t.get("message") or "-",
                                "Measurements Count": len(t.get("measurements", []))
                            })

                        df_tasks = pd.DataFrame(table_rows)
                        st.dataframe(df_tasks, use_container_width=True, hide_index=True)

                        # 각 태스크별 개별 상세 JSON 뷰어
                        st.markdown("##### 🔍 Task별 상세 JSON 및 계측 데이터 (`measurements` 등)")
                        for idx, t in enumerate(task_results, 1):
                            t_status = t.get("status", "N/A")
                            icon = "🔴" if t_status == "Failed" else "🟢"
                            with st.expander(f"{icon} [{idx}] Task: {t.get('name')} | Status: {t_status} | Duration: {t.get('duration_ms')}ms"):
                                # 주요 정보 요약
                                if t.get("message"):
                                    st.markdown(f"**Message:** `{t.get('message')}`")
                                if t.get("measurements"):
                                    st.markdown(f"**Measurements ({len(t.get('measurements'))}건):**")
                                    st.json(t.get("measurements"))
                                
                                st.markdown("**전체 Task JSON 속성:**")
                                st.json(t)

                    else:
                        # 전체 원본 JSON 모드
                        st.json(task_results)

                    # 실패 태스크 경고 박스
                    failed_tasks = [t for t in task_results if t.get("status") == "Failed"]
                    if failed_tasks:
                        for ft in failed_tasks:
                            st.error(f"🚨 **실패 Task:** `{ft.get('name')}` | **원인:** {ft.get('message')}")
                else:
                    st.info("기록된 task_results가 없습니다.")

                st.divider()

        # -------------------------------------------------------------
        # 로그 분석 렌더링 섹션
        # -------------------------------------------------------------
        if content_lines:
            col_filter, col_lines = st.columns([2, 1])
            with col_filter:
                view_mode = st.radio(
                    "로그 분석 뷰 모드 선택:",
                    ["전체 로그 (All)", "Vision / IV2 / OCR 검증 (Target Only)", "일반 시퀀스 에러 (General Only)"],
                    horizontal=True
                )
            with col_lines:
                context_count = st.slider("에러 직전 로그 라인 수 (Context)", min_value=10, max_value=100, value=40, step=5)

            mode_map = {
                "전체 로그 (All)": "all",
                "Vision / IV2 / OCR 검증 (Target Only)": "ocr",
                "일반 시퀀스 에러 (General Only)": "general"
            }

            all_results, filtered_results = parse_log_content(
                content_lines,
                image_keys=list(image_cache.keys()),
                log_type=mode_map[view_mode],
                context_line_count=context_count
            )

            iv2_count = sum(1 for r in all_results if r.get("is_iv2_error"))
            ocr_count = sum(1 for r in all_results if r["is_ocr_error"] and not r.get("is_iv2_error"))
            gen_count = len(all_results) - (iv2_count + ocr_count)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("총 에러 감지 (중복제거)", f"{len(all_results)} 건")
            m2.metric("IV2 LED/Display 에러", f"{iv2_count} 건")
            m3.metric("Vision/OCR 에러", f"{ocr_count} 건")
            m4.metric("일반 시퀀스 에러", f"{gen_count} 건")

            st.divider()

            if not filtered_results:
                st.info("선택한 필터 조건에 해당하는 에러가 없습니다. (테스트 PASS)")
            else:
                for idx, item in enumerate(filtered_results, 1):
                    badge_color = "🟣" if item.get("is_iv2_error") else ("🔴" if item["is_ocr_error"] else "🔵")
                    linked_imgs = item["linked_images"]

                    with st.expander(
                        f"{badge_color} [{idx}] Line {item['line_no']} | {item['category']} | Task: {item['task_name']} | SN: {item['sn']} (PN: {item['pn']})", 
                        expanded=(idx == 1)
                    ):
                        st.markdown(f"**Task / Error 요약:** `{item['summary']}`")

                        if item["root_errors"]:
                            st.error(f"**Root Failure / Error:**\n" + "\n".join([f"- {e}" for e in item["root_errors"]]))

                        if item.get("measurement") or item.get("led_state"):
                            st.markdown("##### 📊 IV2 계측 및 하드웨어 정황 데이터")
                            m_col1, m_col2 = st.columns(2)
                            with m_col1:
                                st.info(f"**계측 결과 (Measurement):**\n`{item['measurement'] or 'N/A'}`")
                            with m_col2:
                                st.info(f"**LED 제어 상태 (LED State):**\n`{item['led_state'] or 'N/A'}`")

                        if item["is_ocr_error"]:
                            st.markdown("##### 📷 Vision / IV2 연계 이미지")
                            if item["ux_action"]:
                                st.caption(f"**UX Action Verify:** `{item['ux_action']}`")

                            if linked_imgs:
                                cols = st.columns(len(linked_imgs))
                                for c_idx, img_info in enumerate(linked_imgs):
                                    with cols[c_idx]:
                                        fname = img_info['filename']
                                        source_tag = img_info.get("source", "IV2")

                                        if "_graph" in fname.lower():
                                            title_tag = f"📈 [{source_tag}] 파형 그래프"
                                        elif "_pass" in fname.lower():
                                            title_tag = f"✅ [{source_tag}] 판정 PASS 이미지"
                                        elif "_fail" in fname.lower():
                                            title_tag = f"❌ [{source_tag}] 판정 FAIL 이미지"
                                        else:
                                            title_tag = f"🖼️ [{source_tag}] {c_idx+1}순위 이미지"

                                        st.markdown(f"**{title_tag}**")
                                        st.caption(f"파일명: `{fname}`")

                                        if fname in image_cache:
                                            img_bytes = image_cache[fname]
                                            st.image(img_bytes, caption=fname, use_container_width=True)
                                            st.download_button(
                                                label=f"💾 {fname} 다운로드",
                                                data=img_bytes,
                                                file_name=fname,
                                                mime="image/jpeg",
                                                key=f"dl_{idx}_{c_idx}"
                                            )
                                        else:
                                            st.warning(f"⚠️ 압축 파일 내에 `{fname}` 바이너리 없음")
                            else:
                                st.write("연계된 이미지 파일이 없습니다.")

                        tab_context, tab_traceback = st.tabs([
                            f"📋 에러 직전 로그 ({len(item['prior_logs'])} 라인)", 
                            "🔍 Traceback 전체 스택"
                        ])

                        with tab_context:
                            if item["prior_logs"]:
                                st.code("\n".join(item["prior_logs"]), language="text")
                            else:
                                st.write("표시할 이전 로그가 없습니다.")

                        with tab_traceback:
                            if item["traceback"]:
                                st.code("\n".join(item["traceback"]), language="python")
                            else:
                                st.write("Traceback 로그가 없습니다.")

            st.sidebar.header("📥 결과 내보내기")
            json_str = json.dumps(filtered_results, indent=2, ensure_ascii=False)
            st.sidebar.download_button(
                label="분석 결과 JSON 다운로드",
                data=json_str,
                file_name=f"{Path(uploaded_file.name).stem}_analyzed.json",
                mime="application/json"
            )
