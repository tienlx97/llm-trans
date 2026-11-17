$vol = "web_syosetu_n0022gd_0001-0938_vi"
$chapters = 43..60 | ForEach-Object { "CHAPTER_{0:D2}" -f $_ }

python scripts/mtl.py bible-vi $vol --analyze --provider codex --chapters $chapters

if ($LASTEXITCODE -eq 0) {
    python scripts/mtl.py translate $vol --provider codex --chapters $chapters
}

# Bạn chỉ cần **đợi hạn mức Codex phục hồi rồi chạy lại đúng script**:

# ```powershell
# .\translate-chap.ps1
# ```

# Giữ nguyên phạm vi `1..28`. Chương trình sẽ:

# - **Bible:** bỏ qua các chương đã phân tích thành công và nội dung không đổi.
# - **Dịch:** bỏ qua các chương đã được đánh dấu `completed` trong manifest.
# - **Chương 4:** nếu đang dịch dở khi hết hạn mức thì chạy lại chương 4; nếu đã hoàn thành thì bắt đầu từ chương 5.

# Ví dụ:

# ```text
# Chương 1–3: hoàn thành → bỏ qua
# Chương 4: bị ngắt      → dịch lại
# Chương 5–28: chưa dịch → tiếp tục lần lượt
# ```

# Chương đang dở có thể phải dịch lại từ đầu chương, không bảo đảm tiếp tục đúng đoạn đang viết.

# Bạn có thể xem tiến độ trước khi chạy lại:

# ```powershell
# python scripts/mtl.py status web_syosetu_n0022gd_0001-0938_vi
# ```

# **Không dùng `resume <vol>` nếu vẫn muốn giới hạn tới chương 28**, vì lệnh đó sẽ tiếp tục qua toàn bộ chương chưa dịch trong 938 chương.