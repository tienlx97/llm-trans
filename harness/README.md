# Coding-agent harness

Quy trình cho agent lập trình dự án: đọc trạng thái → chọn việc → thực hiện →
kiểm chứng → ghi bàn giao, dựa trên
[Learn Harness Engineering](https://walkinglabs.github.io/learn-harness-engineering/en/).
Chỉ dùng file trong repository; không cần database hoặc dịch vụ bên ngoài.

## Thành phần

| Tệp / thư mục | Vai trò |
|---|---|
| `AGENTS.md` / `CLAUDE.md` | Điểm vào và quy tắc chung |
| `harness/features.json` | Tiêu chí nghiệm thu, trạng thái và bằng chứng |
| `harness/progress.md` | Tiến độ đã kiểm chứng |
| `harness/handoff.md` | Bàn giao và bước tiếp theo |
| `harness/decisions.md` | Quyết định kỹ thuật |
| `scripts/harness.py` | Khởi động, kiểm chứng và quản lý feature |
| `.harness/runs/` | Báo cáo local, bỏ qua bởi Git |

## Làm việc

Từ thư mục gốc dự án, dùng Python của môi trường dev:

```powershell
python scripts/harness.py start
python scripts/harness.py doctor
```

`start` kiểm tra cấu trúc, in tiến độ, bàn giao, feature state và các thay đổi
đang có. Không cài dependency, gọi model hoặc sửa dữ liệu dịch. Đọc source
hiện tại để kiểm chứng các ghi chú từ phiên trước.

Sau khi sửa code:

```powershell
python scripts/harness.py verify --profile python
# Khi sửa console, sau khi cài dependency trong ts/:
python scripts/harness.py verify --profile all
```

Profile `python` chạy doctor và pytest offline. Profile `all` thêm typecheck và
test TypeScript. Báo cáo JSON lưu lệnh, exit code, output, thời gian, Git HEAD
và hash source trước/sau vào `.harness/runs/<run-id>/report.json`.

Sau khi kiểm tra đủ tiêu chí nghiệm thu:

```powershell
python scripts/harness.py feature coding-harness --status passing --evidence .harness/runs/<run-id>/report.json
```

Lệnh từ chối bằng chứng thất bại, thiếu check hoặc khác source hiện tại. Hash
bao gồm file tracked, thay đổi chưa commit và file mới không bị ignore. Ba tệp
trạng thái `features.json`, `progress.md`, `handoff.md` được loại khỏi hash để
có thể cập nhật bàn giao sau khi kiểm thử. Giữ tối đa một feature `in_progress`.
Không xóa diff cũ để tạo trạng thái sạch giả.

## Bàn giao

Cập nhật progress/handoff bằng thay đổi thực tế, kết quả kiểm thử, giới hạn và
bước tiếp theo. Các ghi chú được đọc trực tiếp trong phiên sau, không có index
hoặc cơ chế tự động trích xuất. Không đưa khóa API, nội dung truyện hoặc toàn bộ
transcript vào tài liệu tracked. Pipeline dịch và bible nhân vật không thay đổi.
