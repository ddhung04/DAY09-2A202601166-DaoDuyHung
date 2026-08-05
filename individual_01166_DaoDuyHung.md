# Báo cáo cá nhân — Day 9: Multi-Agent A2A

## 1. Thông tin cá nhân

| Thông tin | Nội dung |
| --- | --- |
| Họ và tên | Đào Duy Hưng |
| MSSV | 2A202601166 |
| Khóa/Lớp | K4 |
| Vai trò chính | Thiết kế và triển khai pipeline điều phối, policy và verifier |

## 2. Phạm vi công việc sở hữu

| Module/deliverable | File phụ trách | Input | Output | Trạng thái |
| --- | --- | --- | --- | --- |
| Điều phối và xử lý case | `src/dispute_resolution/engine.py` | JSON case, CSV Olist | JSON theo output schema | Hoàn thành |
| CLI, batch run và trace | `src/dispute_resolution/cli.py` | 50 input cases | 50 output JSON, trace JSONL | Hoàn thành |
| Kiểm tra đầu vào/đầu ra | `cli.py`, `tests/test_preflight.py` | Source data và candidate output | Báo lỗi hoặc xác nhận hợp lệ | Hoàn thành |
| Kiến trúc và metadata | `architecture.md`, `logging/metadata.json` | Quy định đề bài | Tài liệu và metadata runtime | Hoàn thành |

## 3. Kết quả bàn giao

- Pipeline đọc 9 CSV, join dữ liệu theo `order_id`, `customer_id`, `product_id` và `seller_id`.
- 50 file `output/EC_001.json` đến `output/EC_050.json` được tạo bằng `EC_POLICY_V2`.
- `logging/trace.jsonl` có 350 sự kiện: 7 handoff/decision thực thi cho mỗi case.
- Lệnh `python -m dispute_resolution.cli verify` tính lại toàn bộ case và so sánh output đã lưu với kết quả mới.

## 4. Giải thích kỹ thuật

### Vấn đề giải quyết

Mỗi khiếu nại chỉ cung cấp `claimed_order_id`; hệ thống phải đối soát độc lập tình trạng order, item, seller, payment, khách hàng và thời gian giao để kết luận có hoàn tiền hay không. Các kết luận không được dựa trên thông tin không tồn tại trong dữ liệu Olist.

### Cách triển khai

`CaseResolver` đóng vai trò coordinator. Các hàm `customer_agent`, `order_product_agent`, `payment_agent` và `delivery_agent` tạo các handoff dữ liệu có cấu trúc. `policy_agent` áp dụng thứ tự ưu tiên của `EC_POLICY_V2`; `validate_output` chặn output vượt giới hạn, confidence sai hoặc evidence ID sai prefix. Mọi phép tính tiền dùng `Decimal`; variance giao hàng dùng timestamp CSV và được làm tròn hai chữ số.

| Thành phần | Contract |
| --- | --- |
| Input | Một JSON case hợp lệ với `claimed_order_id` và `EC_POLICY_V2` |
| Output | Một JSON theo schema đề bài, không chứa dữ liệu suy diễn |
| Phụ thuộc | CSV trong `data/` và JSON trong `input/` |
| Nơi dùng output | `output/`, trace và bước đóng gói nộp bài |
| Lỗi xử lý | Thiếu dataset/case, JSON sai, order/customer không tồn tại, vượt schema limit |

### Cách xác minh

```powershell
$env:PYTHONPATH='src'
python -m dispute_resolution.cli preflight
python -m dispute_resolution.cli run
python -m dispute_resolution.cli verify
```

Kết quả mong đợi và đã xác minh: 9 dataset, 50 input, 50 output hợp lệ và 350 sự kiện trace.

## 5. Quyết định kỹ thuật quan trọng

- **Bối cảnh:** Các trường thời gian và số tiền phải khớp tuyệt đối với CSV; LLM có thể tạo bằng chứng không tồn tại.
- **Phương án cân nhắc:** Dùng LLM cho toàn bộ suy luận; hoặc dùng rule engine xác định, LLM chỉ để diễn giải.
- **Phương án chọn:** Rule engine Python cho toàn bộ join, tính toán, policy, ID và output.
- **Lý do:** Tái lập được kết quả, tránh hallucination và đảm bảo quy tắc ưu tiên được áp dụng chính xác.
- **Bằng chứng:** Lệnh `verify` tính lại và so sánh cấu trúc dữ liệu của cả 50 output.

## 6. Lỗi đã xử lý

- **Triệu chứng:** Batch đầu tiên dừng với `KeyError: product_category_name`.
- **Nguyên nhân gốc:** Header của `product_category_name_translation.csv` có UTF-8 BOM.
- **Cách xử lý:** Loader đọc CSV bằng `utf-8-sig`, tương thích cả UTF-8 thường và UTF-8 có BOM.
- **Xác minh:** Batch sau đó sinh đủ 50 output; lệnh verifier xác nhận toàn bộ kết quả.
- **Bài học:** Cần chuẩn hóa encoding ở lớp ingest trước khi áp dụng join theo tên cột.

## 7. Hiểu biết end-to-end

Case JSON đi vào coordinator, sau đó các agent chuyên trách truy xuất dữ liệu chỉ đọc và gửi handoff có cấu trúc. Policy agent chọn primary issue theo thứ tự ưu tiên, tạo refund/actions; verifier kiểm tra giới hạn mảng, evidence, confidence và null handling trước khi ghi output. Trace lưu thứ tự coordinator → customer → order-product → payment → delivery → policy → verifier cho từng case. Chất lượng được đo bằng khả năng tái tính 50 output từ dữ liệu gốc và xác minh không có lệch kết quả.

## 8. Cam kết

- [x] Nội dung phản ánh đúng phần việc đã thực hiện.
- [x] Có thể giải thích luồng end-to-end.
- [x] Chỉ ghi kết quả đã được chạy và xác minh.
- [x] Báo cáo không chứa API key, token hoặc secret.
