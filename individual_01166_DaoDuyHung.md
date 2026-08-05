# Báo cáo cá nhân — Day 9: Multi-Agent A2A

## 1. Thông tin cá nhân

| Thông tin | Nội dung |
| --- | --- |
| Họ và tên | Đào Duy Hưng |
| MSSV | 2A202601166 |
| Khóa/Lớp | K4 |
| Vai trò chính | Thiết kế coordinator, policy engine, verifier và batch pipeline |
| Ngày hoàn thành | 2026-08-05 |

## 2. Vai trò và phạm vi công việc

| Module/deliverable | File/hàm phụ trách | Input nhận vào | Output bàn giao | Trạng thái |
| --- | --- | --- | --- | --- |
| Data ingest và lookup | `OlistData.from_directory` | 9 CSV Olist | Lookup theo order/customer/product | Hoàn thành |
| Agent orchestration | `CaseResolver` và specialist classes | Một case JSON | Candidate output đã tổng hợp | Hoàn thành |
| Policy V2 | `PolicyAgent.decide` | Handoff đã kiểm chứng | Issue, party, refund, actions | Hoàn thành |
| Hard gate | `VerifierAgent`, `validate_output` | Candidate output | Pass hoặc lỗi chi tiết | Hoàn thành |
| Batch/trace/package | `cli.py` | 50 input cases | 50 JSON, trace, `output.zip` | Hoàn thành |
| Tài liệu | `architecture.md`, report, metadata | Source và kết quả chạy | Tài liệu bàn giao | Hoàn thành |

## 3. Kết quả theo vai trò

| Nhiệm vụ | Artifact | Kết quả | Cách xác minh |
| --- | --- | --- | --- |
| Join và xử lý 50 case | `output/EC_001.json`–`EC_050.json` | Đủ 50 output | CLI `verify` |
| Bao phủ taxonomy | 6 primary issue | 8 canceled, 6 unavailable, 10 seller, 10 logistics, 8 split, 8 unsupported | Unit test distribution |
| Trace A2A | `logging/trace.jsonl` | 350 event, 7 event/case | CLI kiểm tra sequence/agent/model |
| Gói nộp | `output.zip` | Đúng 50 JSON, không entry lạ | Đọc lại ZIP entry list |

Artifact chính là pipeline có thể tái chạy từ dữ liệu gốc. Output lưu sẵn không được tin mặc định: verifier tái tính lại và đối chiếu từng file với source trước khi đóng gói.

## 4. Giải thích kỹ thuật

### Vấn đề cần giải quyết

Mỗi yêu cầu chỉ cung cấp một `claimed_order_id`. Hệ thống phải điều tra nhiều domain dữ liệu để phân biệt đơn hủy/không khả dụng đã trả tiền, giao trễ do seller, giao trễ do logistics, split payment hợp lệ và claim giao trễ không được dữ liệu hỗ trợ. Kết quả phải chứa đầy đủ context, evidence và số tiền mà không tạo fact ngoài CSV.

### Cách triển khai

CSV được stream một lần để tạo các index dùng chung cho 50 case. Coordinator chuyển cùng order cho bốn specialist agent. Payment dùng `Decimal`; Delivery dùng timestamp gốc; OrderProduct giữ stable source order và dịch category sang English qua bảng translation. PolicyAgent áp dụng precedence tường minh và ném `PolicyError` nếu không nhánh nào khớp. Verifier kiểm tra schema và quan hệ chéo trước khi file được ghi.

### Input, output và contract

| Thành phần | Mô tả |
| --- | --- |
| Input | `input/EC_NNN.json`, 9 CSV trong `data/` |
| Output | JSON đúng schema đề bài, tối đa theo các limit được quy định |
| Module phụ thuộc | Python 3.11 standard library; không có runtime dependency ngoài |
| Module sử dụng output | Verifier, trace writer và ZIP packager |
| Điều kiện lỗi | Thiếu file/case, sai policy/scope, order/customer không tồn tại, policy unmatched, schema/evidence/refund sai |

### Cách xác minh

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
python -m dispute_resolution.cli run
python -m dispute_resolution.cli verify
python -m dispute_resolution.cli package
```

- **Kết quả mong đợi:** 6 test pass; 50 output hợp lệ; 350 trace event; ZIP có 50 JSON.
- **Kết quả thực tế:** đạt đủ bốn điều kiện trên.
- **Artifact/log:** `output/`, `logging/trace.jsonl`, `output.zip`.

## 5. Quyết định kỹ thuật quan trọng

- **Bối cảnh:** Bài toán cần độ đúng số học và evidence cao hơn khả năng diễn giải ngôn ngữ tự nhiên.
- **Phương án cân nhắc:** (1) cho LLM đọc và quyết định toàn bộ; (2) dùng LLM điều phối tool; (3) dùng deterministic multi-agent rule engine.
- **Phương án chọn:** Deterministic multi-agent rule engine, không dùng LLM.
- **Lý do:** Kết quả tái lập, parameter size bằng 0 nên đáp ứng giới hạn 10B, không tốn API, tránh hallucination và dễ kiểm chứng từng rule.
- **Bằng chứng:** Cả 50 case thỏa đúng một nhánh policy tường minh; verifier tái tính và source-audit trước khi package.

## 6. Lỗi hoặc blocker đã xử lý

- **Triệu chứng:** Lần chạy đầu báo `KeyError: product_category_name`.
- **Bước tái hiện:** Đọc file `product_category_name_translation.csv` bằng encoding UTF-8 thông thường và truy cập header.
- **Nguyên nhân gốc:** File translation có UTF-8 BOM ở tên cột đầu tiên.
- **Cách xử lý:** Loader dùng `utf-8-sig`, tương thích cả file có và không có BOM.
- **Xác minh sau sửa:** 50 case load, resolve và verify thành công.
- **Điều học được:** Encoding phải được chuẩn hóa tại data boundary, trước mọi join theo column name.

## 7. Hiểu biết về luồng end-to-end

Input case đi vào coordinator. CustomerAgent tìm identity/history; OrderProductAgent lấy entity và category; PaymentAgent đối soát tiền; DeliveryAgent tính variance. PolicyAgent áp dụng thứ tự ưu tiên và tạo resolution. Verifier kiểm tra hard gate rồi output/trace mới được ghi. Lệnh `verify` không chỉ parse JSON mà còn tái tính và kiểm tra entity, customer/product context, primary/secondary predicates, refund và evidence với CSV. Lệnh `package` chỉ chạy sau khi verify pass và tạo ZIP bằng danh sách trắng 50 filename.

## 8. Cam kết

- [x] Nội dung phản ánh đúng phần việc và mức hiểu của tôi.
- [x] Tôi có thể giải thích luồng end-to-end và từng handoff.
- [x] Không ghi “đã chạy thành công” cho phần chưa kiểm chứng.
- [x] Báo cáo không chứa `.env`, API key, token hoặc secret.
- [x] Báo cáo không sao chép báo cáo thành viên khác.

**Họ và tên:** Đào Duy Hưng  
**Ngày xác nhận:** 2026-08-05
