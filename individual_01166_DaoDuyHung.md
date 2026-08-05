# Báo cáo cá nhân — Day 9: Multi-Agent A2A

## 1. Thông tin cá nhân

| Thông tin | Nội dung |
| --- | --- |
| Họ và tên | Đào Duy Hưng |
| MSSV | 2A202601166 |
| Khóa/Lớp | K4 |
| Vai trò | Thiết kế pipeline, Policy AI, verifier và bộ đóng gói |

## 2. Phạm vi thực hiện

| Hạng mục | File chính | Kết quả |
| --- | --- | --- |
| Data agents và coordinator | `src/dispute_resolution/engine.py` | Join CSV, đối soát tiền/giao hàng, compose đúng schema |
| Policy AI | `src/dispute_resolution/ai_policy.py` | Model 8B chọn primary; policy catalog dựng output không dùng nhánh phân loại `if/else` |
| Batch, trace, metadata, ZIP | `src/dispute_resolution/cli.py` | Chạy 50 case, audit 350 event và ZIP đúng internal path |
| Kiểm thử | `tests/test_pipeline.py` | Test offline bằng recorded decision, không tốn quota |
| Tài liệu | `architecture.md`, báo cáo này | Mô tả vai trò, quyền và handoff end-to-end |

## 3. Giải pháp kỹ thuật

`CaseResolver` điều phối bốn agent chuyên trách. CustomerAgent tra danh tính và lịch sử khách; OrderProductAgent xác định item, seller và category; PaymentAgent tính tiền bằng `Decimal`; DeliveryAgent tính chênh lệch giờ giao và seller handoff. Các facts đã kiểm chứng được gửi sang PolicyAgent.

PolicyAgent sử dụng `llama-3.1-8b-instant` qua Groq. Đây là model 8B, đáp ứng giới hạn mỗi agent không quá 10B. Model nhận sáu condition flags đã kiểm chứng theo đúng precedence và trả JSON primary classification. Policy catalog dạng dữ liệu ánh xạ primary sang cause, party, refund và actions; không có chuỗi `if/else` hardcode để chọn case. Model không được phép tạo entity ID, timestamp hay payment amount; các trường đó do specialist lấy trực tiếp từ CSV.

Verifier có hai lớp:

1. Schema/cross-field validation kiểm type, enum, array limit, root-cause mapping, evidence format và refund/status.
2. Source consistency audit đối chiếu entity, customer/product context, delivery/payment facts và policy predicate trực tiếp với CSV.

Các check này là guardrail, không thay AI đưa ra primary decision. Nếu AI chọn sai precedence, batch dừng thay vì âm thầm đổi nhãn.

## 4. Tối ưu cho free API

- Không thêm dependency runtime; gọi HTTPS bằng Python standard library.
- Chỉ 1 model call/case, tổng 50 calls.
- Prompt chỉ gửi facts cần thiết, không gửi toàn bộ CSV.
- Temperature 0, JSON Object Mode và contract một trường giúp model 8B ổn định.
- Giãn request 7 giây để tránh vượt free-tier token rate; tự retry lỗi 429/network.
- Output chỉ được thay sau khi đủ 50 case pass, tránh làm hỏng bộ kết quả cũ khi hết quota.
- `verify`, unit test và `package` không gọi model.

## 5. Tuân thủ yêu cầu nộp bài

- Model name khai báo trong source và `metadata.json`, không đặt trong `.env`.
- `.env` chỉ chứa API key và bị ignore; trace/metadata không chứa secret.
- `output.zip` chỉ chứa 50 entry từ `output/EC_001.json` đến `output/EC_050.json`.
- Source, `.env`, trace và metadata không nằm trong ZIP.
- Trace lượt mới nhất được ghi đè, không append.
- Không tự commit hoặc push; chỉ thực hiện khi chủ repo yêu cầu rõ.

## 6. Sự cố và bài học

ZIP từng bị từ chối vì JSON nằm ở root archive thay vì dưới prefix `output/`. Packager hiện tạo đúng đường dẫn nội bộ và kiểm lại toàn bộ danh sách entry sau khi nén. Bài học là hard gate cần được kiểm ở artifact cuối, không chỉ ở folder nguồn.

Thiết kế ban đầu dùng rule engine để bảo đảm đúng dữ liệu, nhưng không đáp ứng mong muốn dùng AI thật. Phiên bản hiện tại giữ các specialist và verifier xác định, đồng thời thay riêng logic quyết định policy bằng model 8B. Cách này vừa chứng minh model call thật qua trace, vừa giảm rủi ro hallucination.

## 7. Cách xác minh

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
python -m dispute_resolution.cli preflight
python -m dispute_resolution.cli run
python -m dispute_resolution.cli verify
python -m dispute_resolution.cli package
```

## 8. Kết quả lượt chạy thật

- 50/50 model calls hoàn tất và có response ID trong trace.
- Tổng usage: 9.300 prompt tokens, 592 completion tokens, 9.892 tokens.
- `trace.jsonl` có đúng 350 event của 50 case; file cũ đã được ghi đè.
- 7/7 unit tests pass; lệnh `verify` xác nhận 50 output khớp source và policy.
- `output.zip` có đúng 50 entry từ `output/EC_001.json` đến `output/EC_050.json`, không có file lạ.
- SHA-256 của ZIP: `f7ca9fb85af17fd241d60a66237ae6c4f6ea9de6c2e74a295fa1f268da129fbd`.
