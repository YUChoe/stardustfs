---
inclusion: manual
---

# GUI UI/UX 정리 — Design

## 개요

세 갈래로 나눈다.

1. 레이아웃 — 파일 액션 툴바를 파일 목록 바로 아래로 옮기고, 관리 패널 액션 바가
   사라지는 문제를 고친다. pack 순서를 바로잡고 회귀 테스트로 고정한다.
2. 입력 흐름 정리 — `simpledialog` 연쇄를 폼 다이얼로그로 교체하고, 경로 타이핑을
   선택 UI로 대체한다. 공용 폼 다이얼로그 하나를 만들어 로그인·새 설정·이름 변경이
   함께 쓴다.
3. 표시 정리 — 상태 색·툴팁·빈 상태·정렬·용량 막대·인라인 오류 배너.

기존 모듈 경계(패널별 믹스인)는 유지한다. 새 위젯은 `stardustlib/gui/widgets/`에
모아 각 파일 200줄 이하로 둔다.

## Components and Interfaces

### 레이아웃 (app.py / panel_mgmt.py)

목표 배치:

```
[메뉴바]
[경로 바]
┌ PanedWindow(수직) ────────────────┐
│ [파일 목록]            (expand)    │  ← file_frame
│ [파일 액션 툴바]       (bottom)    │
├ 분할선 ───────────────────────────┤
│ [스토리지·디바이스 헤더] (top)     │  ← mgmt_frame
│ [관리 트리]            (expand)    │
│ [관리 액션 바]         (bottom)    │
└───────────────────────────────────┘
[상태바]
```

파일 액션 툴바는 `body`가 아니라 PanedWindow의 `file_frame` 안으로 옮긴다. 파일
목록에 대한 액션이므로 목록과 같은 pane에 묶여야 분할선을 드래그해도 목록 바로
아래에 남는다.

`expand=True` 컨테이너를 먼저 pack하고 고정 바를 나중에 pack하면, 공간이 모자랄 때
고정 바가 0~1px로 붕괴한다(관리 액션 바의 현재 증상). 세 프레임 모두 같은 규칙을
쓴다 — 고정 바(`side="bottom"`)를 먼저 pack하고 expand 컨테이너를 마지막에 pack.

```python
def _build_body(self) -> None:
    self._build_path_bar(self.body)
    self._build_statusbar(self.body)          # side="bottom"
    paned = ttk.PanedWindow(self.body, orient="vertical")
    paned.pack(fill="both", expand=True)

    file_frame = ttk.Frame(paned)
    self._build_action_toolbar(file_frame)    # side="bottom" — 목록보다 먼저
    self._build_file_tree(file_frame)         # fill="both", expand=True
    paned.add(file_frame, weight=3)

    mgmt_frame = ttk.Frame(paned)
    self._build_mgmt_panel(mgmt_frame)
    paned.add(mgmt_frame, weight=1)

def _build_mgmt_panel(self, parent) -> None:
    self._build_mgmt_header(parent)      # side="top"
    self._build_mgmt_actionbar(parent)   # side="bottom" — 트리보다 먼저
    self._build_mgmt_tree(parent)        # fill="both", expand=True
```

ttk.PanedWindow는 pane별 `minsize`를 지원하지 않으므로(weight만 있다), 분할선을
위로 끌어 툴바가 잘리는 것은 `<ButtonRelease-1>`과 `<B1-Motion>`에서 `sashpos`를
하한으로 클램프해 막는다. 하한은 툴바 요청 높이 + 목록 최소 2행이다.

```python
def _clamp_sash(self, paned) -> None:
    """분할선이 액션 툴바를 덮지 않도록 최소 위치로 되민다."""
    floor = self._toolbar_min_height() + self._row_height() * 2
    if paned.sashpos(0) < floor:
        paned.sashpos(0, floor)
```

### 하단 상태 영역 (statusbar.py)

```python
class StatusBarMixin:
    def _build_statusbar(self, parent) -> None: ...
    def _set_status(self, text: str) -> None: ...
    def _show_banner(self, message: str, *, level: str) -> None:
        """오류/경고를 상태 영역 위 인라인 배너로 표시한다(모달 대체).

        같은 message가 이어지면 새 배너를 쌓지 않고 '(n회)' 카운트만 올린다.
        """
    def _clear_banner(self) -> None: ...
    def _set_progress(self, done: int, total: int) -> None:
        """상태바 오른쪽 진행 막대. total=0이면 막대를 감춘다."""
```

`app._submit`의 실패 처리에서 `messagebox.showerror` → `_show_banner`로 교체한다.
파괴적 동작 확인(`askyesno`)은 모달을 유지한다.

### 공용 폼 다이얼로그 (widgets/form_dialog.py)

```python
@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"      # text | password | path | dir | bool | int
    initial: str = ""
    required: bool = False

class FormDialog:
    """필드 목록을 한 창에 세로로 배치하는 모달 폼.

    submit(values) -> str | None 을 받아 None이면 닫고, 문자열이면 그 메시지를
    창 안 오류 라벨에 표시하고 입력값을 유지한 채 열어 둔다.
    """
    def __init__(self, app, title: str, fields: Sequence[Field], submit) -> None: ...
```

이 다이얼로그로 교체하는 지점:

| 현재 | 교체 |
| --- | --- |
| `session._login`의 `askstring` 3회 | 필드 3개 폼 1창 |
| `session._new_config`의 dialog 4회 | 필드 4개 폼 1창 |
| `file_ops._move`의 경로 입력 | `이름 변경` 폼 + `이동` 폴더 선택 |
| `panel_mgmt._mgmt_add_storage`의 2회 | 경로 + 크기 폼 1창 |
| `UploadDialog`(별도 창) | `askopenfilenames` 1회 + 목록 내 진행 표시 |

### 업로드 흐름 (file_ops.py — upload_dialog.py 삭제)

`UploadDialog`(229줄)를 제거하고 파일 선택 → 목록 내 진행 표시로 대체한다.

```python
@dataclass
class UploadItem:
    """업로드 큐의 한 항목. 목록에 행으로 그려진다."""
    local: str
    name: str
    dest: str                 # 선택 시점의 가상 폴더(고정)
    state: str = "queued"     # queued | running | failed | exists
    error: str = ""

class FileOpsMixin:
    _uploads: list[UploadItem]

    def _upload(self) -> None:
        """파일 선택만 받고 즉시 큐에 넣어 순차 업로드를 시작한다."""
        paths = filedialog.askopenfilenames(parent=self.root, title=self.t["upload_pick"])
        ...
    def _upload_next(self) -> None: ...
    def _cancel_uploads(self) -> None:
        """남은 대기 항목을 큐에서 버린다(진행 중 파일은 끝까지 둔다)."""
```

전송은 기존대로 워커 스레드에서 파일 단위 순차 실행(`actions.put_file`)이다. 한
파일이 끝나면 다음을 큐잉하므로 메인 스레드를 막지 않는다.

목록 표시는 `panel_files._populate` 뒤에 업로드 행을 덧붙이는 방식이다. 서버 목록과
업로드 큐를 한 자료구조에 섞지 않는다 — 폴링 새로고침이 목록을 통째로 다시 그려도
업로드 행이 살아남아야 하기 때문이다.

```python
def _render_uploads(self) -> None:
    """현재 폴더가 대상인 업로드 항목을 목록 상단에 행으로 그린다.

    _populate가 끝날 때마다 호출한다. 이미 그려진 행은 값만 갱신한다.
    """
```

상태는 백업 컬럼 자리에 표시한다(그 파일의 현재 상태를 나타내는 컬럼이므로).
업로드가 끝나면 그 항목을 큐에서 빼고 `_after_write()`로 실제 목록을 갱신한다 —
행이 진행 표시에서 실제 파일로 바뀐다.

진행률의 단위: `actions.put_file`은 파일 단위 원자적 호출이고, 데몬 위임 경로
(`/ctl/put`)도 동기 POST라 바이트 단위 진행을 알 방법이 없다. 따라서 이번 범위는
파일 단위 상태(대기/진행/완료/실패/이미 있음)와 `{i}/{n}` 카운트까지다. 바이트
단위 진행률은 데몬 put 핸들러에 복제와 같은 `ProgressTracker`를 붙이고 `/ctl/progress`가
전송 진행도 싣도록 확장해야 하므로 별도 작업으로 분리한다(범위 밖).

### 가상 폴더 선택 (widgets/vpath_picker.py)

```python
class VPathPicker:
    """가상 경로 트리에서 대상 폴더를 고르는 모달. actions.browse로 폴더만 채운다."""
    def choose(self, start: str) -> str | None: ...
```

### 경로 바 (widgets/breadcrumb.py + panel_files.py)

텍스트 입력 + `이동` 버튼을 브레드크럼으로 대체한다. `self.path_var`(StringVar)를
없애고 `self.vpath`를 경로의 단일 원천으로 삼는다 — 현재는 `refresh()`가
`path_var.get()`으로 vpath를 되읽어 두 값이 어긋날 수 있다.

```python
class Breadcrumb:
    """클릭 가능한 경로 세그먼트 바. 폭을 넘으면 앞쪽을 '…' 메뉴로 접는다."""

    def __init__(self, parent, on_navigate: Callable[[str], None]) -> None: ...
    def set_path(self, vpath: str) -> None: ...
```

세그먼트는 `Toolbutton` 스타일 `ttk.Button`, 구분자는 `›` 라벨이다. 폭 계산은
`<Configure>`에서 다시 하고, 넘치면 루트 다음부터 접어 `…` 버튼 하나로 묶은 뒤
클릭 시 `tk.Menu`로 접힌 경로를 나열한다.

`↑ 상위` 버튼은 유지한다(파일 탐색기와 같다). `_up`, `refresh`, `_on_double`,
`_adopt_config`는 `path_var` 대신 `self.vpath`를 직접 갱신하고 `breadcrumb.set_path`를
호출한다.

### 파일 목록 (panel_files.py)

- `..` 행: `vpath != "/"`이면 `_populate`가 첫 행으로 삽입한다. 행 dict에
  `{"type": "dir", "name": "..", "parent": True}`를 넣고, `_selected_rows()`가
  `parent` 플래그를 가진 행을 걸러 어떤 액션의 대상도 되지 않게 한다(Requirement
  9.7). 더블클릭은 `_on_double`이 `parent` 플래그를 보고 `_up()`을 호출한다.
  정렬(아래)은 `..`를 제외한 뒤 적용하고 항상 맨 앞에 다시 붙인다.

- 정렬: 헤딩 클릭 → `(컬럼, 방향)` 상태를 `self._sort`에 저장, `_populate`에서
  적용. 크기는 원본 바이트, 백업은 상태 순위(완료 > 대기 > 미백업)로 정렬한다.
- 상태 색: `tree.tag_configure`로 `bk_done`/`bk_pending`/`bk_none` 태그에
  `theme.PALETTE` 색 지정, 행 삽입 시 태그 부여.
- 빈 상태: Treeview 위에 겹치는 `ttk.Label`을 `place`로 두고, 행이 0일 때만 표시.
  문구는 사유별(`select_config_hint` / `login_required` / `empty_folder`).
- 정렬 방향 표시: 헤딩 텍스트 뒤에 `▲`/`▼`.

### 툴팁 (widgets/tooltip.py)

```python
def attach(widget, text_fn: Callable[[], str], *, delay_ms: int = 600) -> None:
    """지연 표시 툴팁. text_fn을 표시 시점에 호출해 언어 변경을 따라간다."""
```

툴바 버튼(동작 설명 + 비활성 시 활성 조건), 백업 컬럼(복제본 수 의미), 관리 트리
소스 행(전체 경로)에 붙인다. Treeview는 위젯 단위라 셀 툴팁은 `<Motion>`에서
`identify_row/column`으로 대상을 판별해 문구를 바꾼다.

버튼 툴팁 문구는 `action_defs.Action`의 `key`에서 `tip_{key}`, 활성 조건은
`Need`에서 `need_{need.value}` i18n 키로 만든다 — 액션을 추가할 때 문구가 따라온다.

### 관리 패널 (panel_mgmt.py)

- 소스 표시 이름: `actions.storage_and_devices` 응답의 `kind`와 `path`로
  `루프백 · dev-a.img` 형태를 만든다. 응답에 `path`가 없으면 소스 ID를 유지한다
  (응답 스키마는 구현 시 `act_storage.py`에서 확인 후 확정).
- 용량 막대: `used/total` 비율을 유니코드 블록 문자로 그리는 대신, 컬럼 값에
  `81.0 MiB / 1000.0 MiB (8%)`를 넣고 90% 이상이면 `danger` 태그를 준다
  (Treeway 셀에 위젯을 넣을 수 없으므로 막대는 텍스트 게이지로 대체한다).
- 접기: 헤더에 토글 버튼. 접으면 `paned.forget(mgmt_frame)`, 펴면 다시 `add`하고
  마지막 sash 위치를 복원. 상태는 `prefs`에 `mgmt_collapsed`로 저장.

### 테마 (theme.py)

Accent 버튼이 디자인 시스템 CTA 녹색으로 보이지 않는 것은 sv_ttk가 버튼 배경을
엘리먼트 이미지로 그려 `style.configure(background=...)`가 먹지 않기 때문이다.
`ttk.Style().element_create`로 이미지를 갈아끼우는 대신, 전용 레이아웃을 가진
`Cta.TButton` 스타일을 `clam` 기반으로 정의해 색을 직접 통제한다. 실패 시(테마
미설치 등) 기존 `Accent.TButton`으로 폴백한다.

## Data Models

`prefs`에 추가하는 키(기존 `lang`, `theme`와 같은 파일):

```python
{
    "mgmt_collapsed": bool,   # 관리 패널 접힘
    "sash_ratio": float,      # 마지막 분할선 비율(0.2~0.9)
    "sort": {"col": "name", "desc": False},
    "tray_hint_shown": bool,  # 트레이 최소화 1회 안내 완료
}
```

## Correctness Properties

### Property 1: 고정 바는 잘리지 않는다

*임의의* 창 크기 (w, h), 관리 트리 행 수 n, 분할선 위치 p에 대해 w ≥ 760,
h ≥ 480이면, 상태바·파일 액션 툴바·관리 액션 바의
`winfo_rooty() - root.winfo_rooty() + winfo_height()`는 `root.winfo_height()`
이하이고 각 높이는 24px 이상이다.

### Property 6: 액션 툴바는 파일 목록에 붙어 있다

*임의의* 분할선 위치에 대해, 파일 액션 툴바의 상단은 파일 목록의 하단 이상이고
하단은 스토리지·디바이스 패널의 상단 이하다.

### Property 7: 업로드 행은 새로고침에 살아남는다

*임의의* 업로드 큐 상태와 목록 새로고침 횟수에 대해, 대상 폴더가 현재 폴더인 진행
중(`queued`/`running`) 항목은 새로고침 후에도 목록에 행으로 남아 있다.

### Property 8: `..` 행은 액션 대상이 아니다

*임의의* 선택 상태에 대해, `..` 행은 `_selected_rows()` 결과에 포함되지 않으며,
`..`만 선택한 상태에서는 선택을 요구하는 모든 액션이 비활성이다.

### Property 2: 액션 정의는 하나다

*임의의* 액션 a에 대해, 툴바 버튼·컨텍스트 메뉴 항목의 실행 대상 메서드·활성 조건·
툴팁 문구는 모두 `action_defs`의 같은 `Action` 인스턴스에서 나온다.

### Property 3: 폼 취소는 상태를 바꾸지 않는다

*임의의* 폼 다이얼로그에 대해, 사용자가 취소하거나 창을 닫으면 설정 파일·세션·원격
상태 중 어느 것도 변경되지 않는다.

### Property 4: 언어 전환은 화면 위치를 보존한다

*임의의* 언어 전환에 대해, 전환 후의 현재 가상 경로·정렬 상태·분할선 비율·관리 패널
접힘 상태는 전환 전과 같다.

### Property 5: 비활성 동작은 실행되지 않는다

*임의의* 액션 a와 선택 상태 s에 대해, `is_enabled(a, s) == False`이면 툴바 버튼과
컨텍스트 메뉴 어느 경로로도 a의 실행 메서드가 호출되지 않는다.

## Error Handling

| 상황 | 동작 |
| --- | --- |
| 작업 실패(네트워크·IO) | 인라인 배너(level=error), 상태바 문구는 `err_status` |
| 같은 오류 반복 | 배너 교체 없이 `(n회)` 카운트 갱신 |
| 로그인 실패 | 폼 다이얼로그 유지 + 창 안 오류 라벨, 입력값 보존 |
| 파괴적 동작(삭제·분리) | 기존대로 모달 확인(`askyesno`) 유지 |
| 폴더 선택 중 조회 실패 | 피커 안 오류 라벨, 트리는 마지막 성공 상태 유지 |
| 업로드 실패 | 행에 `실패` 표시(다음 수동 새로고침까지) + 인라인 배너 1건 |
| 업로드 대상 이미 존재 | 덮어쓰지 않고 행에 `이미 있음` 표시, 배너 없음 |
| 툴팁 대상 소멸 | 툴팁 창을 즉시 파괴하고 예외를 삼킨다 |
| `prefs` 손상 | 기본값으로 폴백하고 WARNING 1회 |

## Testing Strategy

- 레이아웃 회귀: 기존 `tests/test_gui_layout.py`의 모듈 공유 Tk 루트를 그대로 쓰고
  760x480·900x620·1400x900에서 Property 1을 검증한다. 관리 트리를 채운 상태를 함께
  검사한다 — 빈 트리에서는 결함이 드러나지 않는다. 또한 `winfo_ismapped()`만으로는
  창 밖으로 밀린 위젯을 잡지 못하므로 좌표·높이로 검사한다.
- 액션 정의: `action_defs`를 순회하며 `Action.method`가 `StardustApp`에 존재하고,
  툴팁 i18n 키(`tip_*`, `need_*`)가 ko/en 양쪽에 있음을 검증(위젯 없이 가능).
- `is_enabled`: 선택 상태 조합별 표 기반 테스트(기존 테스트 확장).
- 폼 다이얼로그: `submit`이 문자열을 반환하면 창이 살아 있고 입력값이 유지되는지,
  `None`이면 닫히는지.
- 정렬: `_populate`에 고정 입력을 주고 정렬 결과 순서를 검증.
- i18n: `_KO`와 `_EN`의 키 집합이 같은지, 신규 키 누락이 없는지(기존 테스트가 있으면
  확장).
