---
inclusion: manual
---

# GUI UI/UX 정리 — Tasks

Phase 1이 확인된 결함 수정이고 나머지는 사용 흐름 개선이다. Phase 1은 단독으로
커밋할 수 있다.

## Phase 1: 하단 영역 배치 정리 (Requirement 1)

- [ ] 1.1 회귀 테스트를 먼저 실패시킨다. `tests/test_gui_layout.py`의
      `test_bottom_bars_survive_small_windows`에 관리 액션 바를 추가하고,
      `winfo_ismapped()` 대신 `y + height <= root height`와 `height >= 24`로
      검사한다(mapped 상태로 붕괴한 위젯을 잡기 위함).
- [ ] 1.2 `app` fixture가 `config_path=None`이라 관리 패널이 빈 상태로만 검사된다.
      `_populate_mgmt`에 고정 응답을 주입해 디바이스·소스가 채워진 상태로 검사하는
      케이스를 추가한다.
- [ ] 1.3 파일 액션 툴바를 `body`에서 PanedWindow의 `file_frame` 안으로 옮긴다
      (`_build_action_toolbar(file_frame)`를 `_build_file_tree`보다 먼저 호출).
      배치는 경로 바 → 파일 목록 → 액션 툴바 → 관리 패널 → 상태바가 된다.
- [ ] 1.4 `panel_mgmt._build_mgmt_panel`을 헤더(top) → 액션 바(bottom) → 트리
      (expand) 순으로 재배치한다. 액션 바가 트리보다 먼저 공간을 잡는다.
- [ ] 1.5 분할선 하한 클램프(`_clamp_sash`)를 `<B1-Motion>`·`<ButtonRelease-1>`에
      건다. ttk.PanedWindow는 pane `minsize`가 없어 드래그로 툴바를 덮을 수 있다.
- [ ] 1.6 테스트 추가: 분할선을 0으로 밀어도 툴바가 온전히 보이는지(Property 1),
      툴바가 파일 목록과 관리 패널 사이에 있는지(Property 6).
- [ ] 1.7 1.1·1.6 테스트가 통과하는지, 실제 창 900x620·760x480에서 배치와 액션 바가
      의도대로인지 스크린샷으로 확인한다. 캡처는 `GetWindowRect` 크기로
      `PrintWindow`한다 — 클라이언트 크기로 잡으면 제목표시줄 높이만큼 하단이 잘려
      상태바가 없는 것처럼 보인다.

## Phase 2: 공용 폼 다이얼로그와 입력 흐름 (Requirement 2, 4)

- [ ] 2.1 `stardustlib/gui/widgets/form_dialog.py` 추가 — `Field` 데이터클래스와
      `FormDialog`(모달, 필드 세로 배치, 창 안 오류 라벨, 입력값 보존).
- [ ] 2.2 `session._login`을 이메일·비밀번호·키 암호 3필드 폼으로 교체. 실패 시
      다이얼로그를 유지하고 사유를 창 안에 표시한다.
- [ ] 2.3 `session._new_config`를 폴더·서버 URL·디바이스 이름·키 생성 여부 4필드
      폼으로 교체.
- [ ] 2.4 상단 바의 로그인/로그아웃 두 버튼을 상태에 따라 하나만 보이는 계정 버튼으로
      바꾸고, 로그인 상태이면 이메일을 함께 표시한다. `actions`에서 현재 계정
      식별자를 읽는 경로를 확인한 뒤 사용한다(없으면 `act_auth`에 조회 추가).
- [ ] 2.5 `panel_mgmt._mgmt_add_storage`의 파일 선택 + 크기 입력 2회를 폼 1창으로
      묶는다.
- [ ] 2.6 `action_defs`에서 `move`를 `rename`(현재 폴더 고정, 이름만 편집)과
      `move`(폴더 선택)로 분리하고 i18n 키를 추가한다.
- [ ] 2.7 `widgets/vpath_picker.py` 추가 — `actions.browse`로 폴더만 채우는 트리
      피커. `_move`, `_copy`가 사용한다.
- [ ] 2.8 테스트: 폼 취소 시 상태 무변경(Property 3), 실패 시 창 유지·입력 보존.

## Phase 2b: 업로드를 파일 선택 + 목록 내 진행으로 (Requirement 8)

- [ ] 2b.1 `file_ops._upload`을 `filedialog.askopenfilenames` 1회로 바꾸고 선택 즉시
      업로드 큐를 시작한다. 대상 폴더는 선택 시점의 `vpath`로 고정한다.
- [ ] 2b.2 `UploadItem` 큐와 순차 실행(`_upload_next`)을 `file_ops`로 옮긴다. 전송은
      기존대로 워커 스레드에서 파일 단위(`actions.put_file`).
- [ ] 2b.3 `panel_files._render_uploads` 추가 — `_populate` 끝에서 호출해 현재 폴더가
      대상인 항목을 행으로 그린다. 상태는 백업 컬럼 자리에 표시한다.
- [ ] 2b.4 완료 항목은 큐에서 빼고 `_after_write()`로 실제 목록을 갱신한다.
      `RemotePathExists`는 `이미 있음`, 실패는 `실패`로 남긴다.
- [ ] 2b.5 컨텍스트 메뉴에 `업로드 취소` 추가 — 남은 대기 항목만 버린다.
- [ ] 2b.6 상태바에 `업로드 중 {i}/{n}: {name}` 표시.
- [ ] 2b.7 `stardustlib/gui/upload_dialog.py` 삭제 + i18n에서 `upload_dlg_*`,
      `upload_log_*` 키 정리, 새 상태 키 추가.
- [ ] 2b.8 테스트: 새로고침 후에도 진행 행이 남는지(Property 7), 대상 폴더가 다르면
      그리지 않는지, 취소가 진행 중 파일을 중단하지 않는지.

## Phase 2c: 경로 이동을 브레드크럼과 `..` 행으로 (Requirement 9)

- [ ] 2c.1 `stardustlib/gui/widgets/breadcrumb.py` 추가 — 클릭 가능한 세그먼트 바,
      폭 초과 시 앞쪽을 `…` 메뉴로 접기(`<Configure>`에서 재계산).
- [ ] 2c.2 `_build_path_bar`에서 Entry와 `이동` 버튼을 제거하고 브레드크럼을 넣는다.
      `↑ 상위`, `새로고침`, 계정 버튼은 유지한다.
- [ ] 2c.3 `self.path_var`를 제거하고 `self.vpath`를 단일 원천으로 만든다. `refresh`,
      `_up`, `_on_double`, `_adopt_config`, `_go`(삭제) 경로를 정리한다.
- [ ] 2c.4 `_populate`가 `vpath != "/"`일 때 첫 행에 `..`(`parent` 플래그)를
      삽입하게 한다. 크기·백업 값은 비운다.
- [ ] 2c.5 `_selected_rows()`가 `parent` 행을 걸러내고, `_on_double`이 `parent` 행에서
      `_up()`을 호출하게 한다.
- [ ] 2c.6 i18n에서 `go` 키를 제거하고 브레드크럼 루트 라벨 키를 추가한다.
- [ ] 2c.7 테스트: 루트에서 `..` 없음, 하위에서 첫 행이 `..`, `..`만 선택 시 액션
      전부 비활성(Property 8), 세그먼트 클릭이 vpath를 바꾸는지.

## Phase 3: 파일 목록 표시 (Requirement 3, 10)

- [ ] 3.1 백업 상태 색 태그(`tree.tag_configure`)와 행 태그 부여. 색은
      `theme.PALETTE`에서 가져온다.
- [ ] 3.2 빈 상태 오버레이 라벨 — 행이 0일 때만 `place`로 표시하고 사유별 문구를
      고른다. i18n에 `empty_folder` 추가.
- [ ] 3.3 컬럼 헤딩 클릭 정렬 + 방향 표시. 크기는 원본 바이트, 백업은 상태 순위로
      정렬한다. `..` 행은 정렬에서 제외하고 항상 첫 행에 붙인다. 정렬 상태를
      `prefs.sort`에 저장·복원.
- [ ] 3.4 헤딩 정렬을 데이터 정렬과 맞춘다(이름 좌, 크기 우, 백업 좌). 다크 테마에서
      컬럼 경계가 붙어 보이지 않도록 헤딩 좌우 여백을 확인한다.
- [ ] 3.5 영어 컬럼 헤딩 대문자 정정(`Name`/`Size`/`Backup`), i18n 키 집합 일치
      테스트 추가.
- [ ] 3.6 테스트: 정렬 결과 순서, 빈 상태 문구 선택 로직.

## Phase 4: 툴팁 (Requirement 3.2, 5)

- [ ] 4.1 `stardustlib/gui/widgets/tooltip.py` 추가(지연 표시, 표시 시점에 문구
      생성해 언어 전환 반영).
- [ ] 4.2 툴바 버튼에 툴팁 부착 — 동작 설명(`tip_{key}`), 비활성이면 활성 조건
      (`need_{need}`)을 덧붙인다. i18n에 해당 키 추가.
- [ ] 4.3 백업 컬럼 셀 툴팁 — `<Motion>`에서 `identify_row/column`으로 대상 판별,
      `(온라인 복제본/필요 복제본)` 의미를 설명한다.
- [ ] 4.4 테스트: `Action.method`가 모두 `StardustApp`에 존재, 툴팁 i18n 키가 ko/en
      양쪽에 있음.

## Phase 5: 관리 패널 가독성 (Requirement 6)

- [ ] 5.1 `act_storage`의 응답 스키마를 확인해 소스의 종류·경로 필드를 확정한다
      (추측 금지). 없으면 응답에 추가한다.
- [ ] 5.2 소스 행 표시를 `루프백 · dev-a.img` 형태로 바꾸고 전체 경로·소스 ID는
      툴팁으로 제공한다.
- [ ] 5.3 용량 컬럼에 사용률 퍼센트를 병기하고 90% 이상이면 경고 색 태그를 준다.
- [ ] 5.4 관리 패널 접기/펴기 토글 + `prefs.mgmt_collapsed` 저장·복원. 펼 때 마지막
      sash 비율(`prefs.sash_ratio`)을 복원한다.
- [ ] 5.5 테스트: 표시 이름 생성 함수, 사용률 임계 태그.

## Phase 6: 피드백 일관성 (Requirement 7)

- [ ] 6.1 `statusbar._show_banner` / `_clear_banner` 구현(레벨별 색, 닫기 버튼,
      같은 메시지 반복 시 횟수 갱신).
- [ ] 6.2 `app._submit` 실패 경로와 각 믹스인의 `messagebox.showerror`를 배너로
      교체한다. 파괴적 확인(`askyesno`)은 유지한다.
- [ ] 6.3 상태바 진행 막대(`_set_progress`) 추가하고 `_show_progress`(복제)와
      업로드 카운트가 함께 쓰게 한다.
- [ ] 6.4 Accent 버튼을 업로드 하나로 줄이고(`backup_now`의 `accent=False`),
      `Cta.TButton` 스타일로 디자인 시스템 CTA 색을 실제로 적용한다. 적용 실패 시
      기존 `Accent.TButton`으로 폴백한다.
- [ ] 6.5 트레이 최소화 1회 안내 — `prefs.tray_hint_shown`으로 최초 1회만 배너로
      알린다.
- [ ] 6.6 테스트: 같은 오류 반복 시 배너가 쌓이지 않고 횟수만 오르는지.

## Phase 7: 언어 전환 상태 보존 (Requirement 8.3)

- [ ] 7.1 `_rebuild_body`가 현재 경로·정렬·분할선 비율·관리 패널 접힘을 저장했다가
      재구성 후 복원하게 한다.
- [ ] 7.2 테스트: 언어 전환 전후 상태 동일(Property 4).

## 검증

- [ ] 8.1 `pytest` 전체 통과.
- [ ] 8.2 실제 GUI를 dev-config로 띄워 900x620·760x480·1400x900에서 스크린샷 확인
      (`docs/TKINTER_MCP.md` 절차). 다크·라이트, ko·en 각각.
- [ ] 8.3 daemon 미실행·비로그인·오프라인 상태에서 화면이 사유를 설명하는지 확인.
