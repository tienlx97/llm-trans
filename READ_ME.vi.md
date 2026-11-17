# LLM Translator — Hướng dẫn tiếng Việt

[English](README.md) | [Tiếng Việt](READ_ME.vi.md)

Dự án dịch light novel tiếng Nhật sang tiếng Việt hoặc tiếng Anh, nhận đầu vào
từ EPUB hoặc URL truyện trên Syosetu/Kakuyomu. Luồng chính: tải/tách chương →
chuẩn bị ngữ cảnh → dịch → kiểm tra chất lượng (QC) → đóng gói EPUB.

Với truyện dài đang ra tiếp, chế độ `--incremental` phân tích nhân vật, xưng hô
và thuật ngữ theo từng nhóm rồi dịch nhóm đó. Bạn có thể dừng và chạy tiếp mà
không phải phân tích lại những chương không đổi đã có mốc thành công.

## Mục lục

- [Cài đặt](#cài-đặt)
- [Chọn provider](#chọn-provider)
- [Dịch nhanh từ EPUB](#dịch-nhanh-từ-epub)
- [Truyện web dài: ví dụ 938 chương](#truyện-web-dài-ví-dụ-938-chương)
- [Tải chương mới và cập nhật truyện](#tải-chương-mới-và-cập-nhật-truyện)
- [Cơ chế lưu mốc phân tích](#cơ-chế-lưu-mốc-phân-tích)
- [Quản lý tên nhân vật, xưng hô và thuật ngữ](#quản-lý-tên-nhân-vật-xưng-hô-và-thuật-ngữ)
- [Kiểm tra và sửa bản dịch](#kiểm-tra-và-sửa-bản-dịch)
- [Lệnh thường dùng](#lệnh-thường-dùng)
- [Cấu hình và thư mục](#cấu-hình-và-thư-mục)
- [Kiểm thử](#kiểm-thử)
- [Harness cho agent lập trình](#harness-cho-agent-lập-trình)
- [Giới hạn và lưu ý](#giới-hạn-và-lưu-ý)

## Cài đặt

Chạy từ thư mục gốc dự án. Trên Windows PowerShell, dùng Python 3.10 trở lên:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/mtl.py --help
```

Nếu PowerShell không cho kích hoạt môi trường, có thể gọi trực tiếp Python của
môi trường, ví dụ `.\.venv\Scripts\python.exe scripts/mtl.py --help`.

Linux/macOS: tạo môi trường bằng `python3 -m venv .venv`, kích hoạt bằng
`source .venv/bin/activate`, rồi cài cùng tệp `requirements.txt`.

Các ví dụ dùng `python scripts/mtl.py` để chọn đúng Python đang kích hoạt.
`mtl.bat` dùng Python hệ thống hoặc đường dẫn trong biến `LLM_TRANSLATOR_PYTHON`;
nó không tự chọn `.venv` của dự án.

Trong các lệnh bên dưới, thay `<url>`, `<vol>` và `<series_id>` bằng giá trị thật.
`<vol>` là tên thư mục workspace bên trong `work/`; dùng lệnh `list` để tìm.

## Chọn provider

| Ngôn ngữ đích | Provider dịch được hỗ trợ |
|---|---|
| Tiếng Việt (`vi`) | `openai`, `gemini`, `codex`, `claude` |
| Tiếng Anh (`en`) | `deepseek`, `qwen`, `openai`, `anthropic`, `gemini`, `codex`, `claude` |

**Dùng tài khoản CLI:** cài và đăng nhập Codex CLI hoặc Claude Code trên máy,
sau đó chọn `--provider codex` hoặc `--provider claude`. Với Codex CLI, đăng nhập
bằng `codex login`; với Claude Code, mở `claude` rồi dùng `/login` khi cần.
Dự án gọi CLI đã đăng nhập; mức sử dụng được tính theo giới hạn tài khoản/gói.
Nếu chọn CLI cho dịch, bước prep mặc định cũng dùng CLI đó, trừ khi bạn chỉ định
`--prep-provider` khác.

**Dùng API:** thêm khóa vào `.env` theo provider, ví dụ `OPENAI_API_KEY` hoặc
`GEMINI_API_KEY`. Giữ các khóa đã có khi sửa tệp và không commit `.env`.
Provider dịch, prep và trợ lý phân tích có thể khác nhau:

| Công việc | Cấu hình / tùy chọn |
|---|---|
| Dịch | `translation.provider`, ghi đè bằng `--provider` |
| Chuẩn bị `context.xml` | `prep.provider`, ghi đè bằng `--prep-provider` |
| Phân tích bible, QC bằng LLM, sửa bằng LLM | `assistant.provider` và tùy chọn provider của lệnh tương ứng |

Ở chế độ incremental, dịch bằng `codex`/`claude` sẽ dùng chính CLI đó để phân
tích bible. Các provider dịch khác dùng `assistant.provider`. Ví dụ, nếu chỉ
dùng OpenAI API, chọn `--provider openai --prep-provider openai` và đặt
`assistant.provider: openai`. Không mặc định rằng chọn provider dịch sẽ thay
luôn cấu hình prep và assistant.

## Dịch nhanh từ EPUB

```powershell
python scripts/mtl.py run "D:\Books\novel.epub" --target vi --provider claude --incremental --window 20
```

Để chạy từng bước:

```powershell
python scripts/mtl.py extract "D:\Books\novel.epub" --target vi
python scripts/mtl.py list
python scripts/mtl.py resume <vol> --provider claude --incremental --window 20
```

EPUB tiếng Việt được ghi vào `output/`. Các chương dịch nằm trong
`work/<vol>/VI/`. Mỗi workspace giữ ngôn ngữ đã chọn lúc tạo; đổi cấu hình mặc
định không chuyển workspace tiếng Anh thành tiếng Việt.

## Truyện web dài: ví dụ 938 chương

Để tải toàn bộ chương hiện có rồi phân tích và dịch theo từng nhóm 20 chương:

```powershell
python scripts/mtl.py run "<url>" --target vi --provider claude --incremental --window 20
```

URL có thể là trang chính của truyện Syosetu hoặc Kakuyomu. URL một chương có
thể chỉ chọn chương đó khi không truyền `--range`; dùng URL trang chính để lấy
cả bộ. Muốn giới hạn đúng 938 chương đầu, thêm `--range 1-938`.

Trình tự xử lý:

1. Tải các chương được chọn và tạo/cập nhật workspace.
2. Chạy prep nếu chưa hoàn tất, tạo ngữ cảnh và khởi tạo bible tiếng Việt.
3. Lấy tối đa 20 chương chưa dịch, theo thứ tự đọc.
4. Phân tích những chương trong nhóm chưa có mốc hoặc đã đổi nội dung; lưu bible.
5. Dịch nhóm này, rồi chuyển sang nhóm tiếp theo.
6. Khi các nhóm hoàn tất, chạy QC và đóng gói EPUB.

`--window 1` xử lý từng chương; `--window 20` là mặc định khi bật incremental.
Nhóm là giới hạn số chương, còn bước phân tích có thể chia nhỏ nhóm theo dung
lượng văn bản. Không có `--incremental`, pipeline giữ thứ tự xử lý thông thường.

**Tải và prep vẫn diễn ra trước khi bắt đầu các nhóm.** Tùy chọn này tránh việc
phải phân tích bible cho toàn bộ 938 chương trước khi dịch; nó không biến bước
tải truyện thành tải từng nhóm. Với CLI, lượng nguồn prep đọc được giới hạn bởi
`cli.prep_max_chars`; lựa chọn nguồn của prep không thay đổi theo `--window`.

Nếu đã tải truyện hoặc bị ngắt giữa chừng:

```powershell
python scripts/mtl.py list
python scripts/mtl.py status <vol>
python scripts/mtl.py resume <vol> --provider claude --incremental --window 20
```

Lặp lại các cờ incremental khi chạy tiếp. Chương đã dịch xong được bỏ qua; nếu
phân tích đã lưu thành công nhưng dịch thất bại, lần tiếp theo dùng lại mốc đó.
Nếu một nhóm còn chương chưa hoàn thành, luồng dừng trước khi phân tích nhóm sau.
`resume` chỉ xử lý dữ liệu đã tải trong workspace, không tự tải chương mới từ web.

## Tải chương mới và cập nhật truyện

Chạy lại lệnh URL để nhận các chương tác giả vừa đăng:

```powershell
python scripts/mtl.py run "<url>" --target vi --provider claude --incremental --window 20
```

Khi cùng truyện, cùng chương bắt đầu và cùng ngôn ngữ, importer tìm lại workspace
cũ và thêm chương mới. Nếu muốn mở rộng phạm vi theo đợt, giữ nguyên chương bắt đầu:

```powershell
python scripts/mtl.py run "<url>" --target vi --range 1-50 --provider claude --incremental --window 20
python scripts/mtl.py run "<url>" --target vi --range 1-100 --provider claude --incremental --window 20
python scripts/mtl.py run "<url>" --target vi --range 1- --provider claude --incremental --window 20
```

Workspace có thể giữ tên ban đầu chứa `0001-0050` dù đã thêm chương sau 50.
`--range 51-100` có chương bắt đầu khác nên thường tạo volume riêng.

Để tải lại cả chương đã cache và phát hiện tác giả sửa nội dung, dùng `--refresh`:

```powershell
python scripts/mtl.py import-url "<url>" --target vi --refresh --run --provider claude --incremental --window 20
```

Chương thay đổi khi đồng bộ được đưa lại về trạng thái chờ dịch. Đừng dùng
`--refresh` cho mọi lần cập nhật nếu chỉ cần lấy chương mới, vì nó tải lại dữ
liệu đã cache. Cú pháp phạm vi hỗ trợ `1-50`, `51-` hoặc `7`.

## Cơ chế lưu mốc phân tích

Mốc nằm trong trường `analysis_chapters` của
`work/<vol>/.context/bible_vi.json`. Mỗi chương có hash SHA-256 của văn bản đã
gửi để phân tích. Mốc và các bổ sung vào bible được lưu cùng một lần ghi atomic
sau mỗi lô thành công.

- Nội dung không đổi và đã phân tích thành công: bỏ qua lời gọi LLM phân tích.
- Nội dung đổi hoặc chưa có mốc: đưa vào lần phân tích tiếp theo khi được chọn.
- Lô bị lỗi: không đánh dấu hoàn thành; có thể chạy lại.
- Dry run: không lưu mốc và không thay đổi bible.
- Tạo lại bible bằng `--seed`: giữ mốc của workspace hiện tại.
- Volume khác trong cùng bộ: không kế thừa mốc phân tích của volume trước.

Bible cũ chưa có lịch sử mốc sẽ phân tích một lần để thiết lập mốc cho những
chương được chọn. Incremental chỉ chọn chương chưa dịch; nó không tự quay lại
phân tích các chương đã dịch hoàn tất trước đây. Hash không thay thế trạng thái
dịch trong manifest: nếu sửa file JP bằng tay, cần reset chương để dịch lại.

Phân tích lại toàn bộ chương được chọn, kể cả nội dung không đổi:

```powershell
python scripts/mtl.py bible-vi <vol> --analyze --force-analyze --provider claude
```

Hoặc giới hạn vào một vài chương, dùng đúng ID theo tên file trong `JP/`:

```powershell
python scripts/mtl.py bible-vi <vol> --analyze --chapters CHAPTER_01 CHAPTER_02 --provider claude
```

Phân tích lại vẫn tuân theo quy tắc bằng chứng và khóa tên; nó không tự xóa mọi
quyết định cũ khi nguồn thay đổi. Không cần bật `auto_analyze` để dùng incremental.
Trong luồng thông thường, nếu prep đã xong và bible tồn tại, `resume` có thể bỏ
qua phân tích; dùng `bible-vi --analyze` để tiếp tục riêng bước này.

## Quản lý tên nhân vật, xưng hô và thuật ngữ

Bible lưu tên gốc, tên dùng trong bản Việt, bí danh, giới tính kèm mức bằng chứng,
quan hệ, cách xưng/gọi theo từng cặp nhân vật, thuật ngữ và các điểm còn mơ hồ.
Mỗi chương nhận thông tin có liên quan đến nguồn và thời điểm xuất hiện.

```powershell
python scripts/mtl.py bible-vi <vol> --show
python scripts/mtl.py bible-vi <vol> --seed
```

Sửa `work/<vol>/.context/bible_vi.json` để chỉnh tên, thuật ngữ hoặc xưng hô.
Sau khi kiểm tra, khóa các tên và thuật ngữ đã có bản Việt:

```powershell
python scripts/mtl.py bible-vi <vol> --lock
```

Lệnh này khóa tất cả tên và thuật ngữ có bản Việt, không chỉ một mục. Bổ sung
sau đó không ghi đè mục đã khóa; bất đồng được ghi lại để người vận hành xem.
Thông tin suy luận không tự ghi đè bằng chứng mạnh hơn; cách xưng hô do LLM đề
xuất được coi là quyết định suy luận, không phải sự thật được tác giả xác nhận.

Nếu chia truyện thành nhiều volume, đưa các quyết định của volume đã duyệt lên
bible chung rồi dùng cùng series ID cho volume tiếp:

```powershell
python scripts/mtl.py bible-vi <vol> --promote --series-id my_series
python scripts/mtl.py run "<url>" --target vi --range 101-200 --series-id my_series --provider claude --incremental --window 20
```

## Kiểm tra và sửa bản dịch

QC mặc định kiểm tra bằng quy tắc và không gọi LLM. Thêm `--llm` để đánh giá
ngữ nghĩa qua provider được chọn; thao tác này dùng API hoặc hạn mức CLI.

```powershell
python scripts/mtl.py qc <vol>
python scripts/mtl.py qc <vol> --llm --provider claude
python scripts/mtl.py repair <vol> --mechanical
python scripts/mtl.py repair <vol> --severity major --provider claude
```

Nếu thiếu đoạn hoặc cần dịch lại một chương:

```powershell
python scripts/mtl.py repair <vol> --reset CHAPTER_01
python scripts/mtl.py resume <vol> --provider claude --incremental --window 20
python scripts/mtl.py qc <vol>
```

Bản dịch cũ khi reset được giữ trong `.context/stale/`. Nếu sửa bản dịch hoặc
bible sau khi đã xuất sách, chạy lại build để cập nhật EPUB:

```powershell
python scripts/mtl.py build <vol>
```

## Lệnh thường dùng

| Lệnh | Mục đích |
|---|---|
| `extract <epub> --target vi` | Tách EPUB thành workspace tiếng Việt |
| `import-url "<url>" --target vi` | Tải truyện web và tạo/cập nhật workspace |
| `prep <vol> --prep-provider P` | Chuẩn bị `context.xml`, khởi tạo bible tiếng Việt |
| `translate <vol> --provider P --chapters CHAPTER_01` | Dịch các chương chỉ định; không tự chạy bước phân tích bible |
| `run "<epub hoặc url>" --target vi --provider P` | Chạy toàn bộ pipeline |
| `resume <vol> --provider P` | Tiếp tục workspace đã có |
| `bible-vi <vol> --analyze --provider P` | Phân tích chương chưa có mốc hoặc nội dung đã đổi |
| `qc <vol>` | Kiểm tra bản dịch |
| `repair <vol> ...` | Sửa có chọn lọc |
| `build <vol>` | Đóng gói EPUB |
| `list` / `status <vol>` | Xem workspace và tiến độ |

Thêm `--incremental --window N` vào `run`, `resume` hoặc `import-url --run` để
dùng luồng phân tích rồi dịch theo nhóm. `N` phải từ 1 trở lên và workspace phải
là tiếng Việt. Xem toàn bộ tùy chọn bằng `python scripts/mtl.py <lệnh> --help`.

Để xem payload mà không gửi yêu cầu dịch/phân tích:

```powershell
python scripts/mtl.py translate <vol> --provider claude --dry-run
python scripts/mtl.py bible-vi <vol> --analyze --provider claude --dry-run
```

Payload nằm trong `work/<vol>/DRY_RUN/`. Phân tích dry run cũng bỏ qua chương
đã có mốc; thêm `--force-analyze` nếu muốn dựng lại payload cho các chương đó.

## Cấu hình và thư mục

Các thiết lập nằm trong `config.yaml`. `project.target_language` là ngôn ngữ mặc
định của workspace mới, hiện được cấu hình là `en`; dùng `--target vi` để ghi đè.
Mỗi provider có cấu hình model, giới hạn sinh, reasoning và cache riêng.

```text
config.yaml                     Cấu hình pipeline và provider
.env                            Khóa API trên máy
raw/                            EPUB nguồn và dữ liệu đầu vào
work/<vol>/JP/                  Chương tiếng Nhật
work/<vol>/VI/                  Chương đã dịch sang tiếng Việt
work/<vol>/manifest.json        Trạng thái từng chương và pipeline
work/<vol>/context.xml          Ngữ cảnh dùng để dịch
work/<vol>/.context/bible_vi.json  Bible và mốc phân tích của volume
work/<vol>/QC/                  Báo cáo QC tiếng Việt
bibles/<series_id>/vi/          Bible tiếng Việt dùng chung giữa các volume
output/                         EPUB đầu ra
scripts/mtl.py                  CLI Python
ts/                             Giao diện terminal TypeScript tùy chọn
```

Giao diện terminal: cài dependency bằng `npm install` trong `ts/`, rồi chạy
`mtl-ts.bat` hoặc `mtl-ts.sh` từ thư mục gốc. Các ví dụ incremental ở đây dùng
trực tiếp CLI Python. Tài liệu kiến trúc, chi tiết provider, MCP và định dạng
`context.xml` nằm trong [README tiếng Anh](README.md) và
[hướng dẫn giao diện terminal](ts/README.md).

## Kiểm thử

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Bộ test mặc định chạy offline, dùng client giả lập và thư mục tạm; không gọi
API trả phí. Kiểm tra truy cập web thật chỉ chạy khi bật `MTL_LIVE_NETWORK=1`.
Các test incremental bao gồm lưu mốc khi bị ngắt, chạy tiếp, nguồn thay đổi,
dry run, thứ tự chương 9 → 10 → 100 và dừng khi nhóm còn chương chưa xong.

## Harness cho agent lập trình

Agent đọc [AGENTS.md](AGENTS.md) hoặc [CLAUDE.md](CLAUDE.md), sau đó dùng trạng
thái tính năng, tiến độ và bàn giao trong `harness/` để tiếp tục công việc.
Chỉ dùng file trong repository, không cần database, chỉ mục hay dịch vụ bên ngoài.

```powershell
python scripts/harness.py start
python scripts/harness.py verify --profile python
```

Các lệnh kiểm chứng lưu báo cáo cùng hash source trong `.harness/runs/`, giúp
tránh đánh dấu hoàn thành khi test lỗi hoặc kết quả đã cũ. Khi sửa console,
cài dependency trong `ts/` rồi dùng `verify --profile all`.

Cập nhật `harness/progress.md` và `harness/handoff.md` sau mỗi phiên. Pipeline
dịch, bible nhân vật và checkpoint tiếp tục hoạt động như trước.
Xem [hướng dẫn harness](harness/README.md) để biết quy trình và cách lưu bằng chứng.

## Giới hạn và lưu ý

- Import web hiện hỗ trợ Syosetu và Kakuyomu; tôn trọng robots.txt, không vượt
  đăng nhập, CAPTCHA, paywall hoặc cổng xác nhận tuổi.
- Incremental hiện dành cho tiếng Việt. Một workspace có thể chứa cả bộ truyện;
  `--window` không tự chia thành nhiều EPUB.
- `--skip-prep` của `resume` bỏ qua chuẩn bị ngữ cảnh; nên dùng khi bạn hiểu rõ
  trạng thái workspace và chấp nhận ảnh hưởng đến tính nhất quán bản dịch.
- Pipeline có thể tiếp tục build sau khi báo QC không đạt; hãy đọc báo cáo QC
  trước khi coi EPUB là bản đã duyệt.
- Tên, cách xưng hô và bằng chứng do LLM đề xuất vẫn cần người đọc kiểm tra.
  Khóa tên sau khi duyệt giúp giữ quyết định ổn định trong những lần bổ sung sau.
