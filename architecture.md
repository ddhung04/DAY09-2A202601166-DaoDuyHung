# Kiến trúc hệ thống — Olist Dispute Resolution

## 1. Mục tiêu thiết kế

Hệ thống xử lý độc lập 50 case theo `EC_POLICY_V2`. Mọi phép join, tính tiền, tính thời gian, evidence ID và quyết định hoàn tiền đều được thực hiện bằng rule engine xác định. Hệ thống không dùng LLM, vì vậy số parameter là 0 và không có nguy cơ model tạo sự kiện không tồn tại trong CSV.

Các nguyên tắc bắt buộc:

- `data/` và `input/` chỉ được đọc.
- Dùng `Decimal` cho tiền và làm tròn `ROUND_HALF_UP` đến 2 chữ số.
- Timestamp giữ nguyên định dạng nguồn; không chuyển múi giờ.
- Policy áp dụng theo đúng thứ tự ưu tiên, không có fallback âm thầm.
- Verifier phải pass trước khi ghi output.
- Mỗi batch ghi đè trace của lượt chạy trước, không append.

## 2. Sơ đồ agent và handoff

```text
input/EC_XXX.json
        |
        v
Coordinator (CaseResolver)
        |
        +----> CustomerAgent --------> customer_unique_id, related_order_ids
        |
        +----> OrderProductAgent ----> item/seller IDs, product/category context
        |
        +----> PaymentAgent ---------> totals, difference, reconciled, split payment
        |
        +----> DeliveryAgent --------> delivery variance, seller handoff variance
        |                                      |
        +----------------------+---------------+
                               v
                         PolicyAgent
                primary/secondary, party, refund,
                    root cause và ordered actions
                               |
                               v
                         VerifierAgent
                  schema + limits + cross-fields
                         /                 \
                        v                   v
             output/EC_XXX.json     logging/trace.jsonl
```

Mỗi case tạo đúng 7 trace event theo thứ tự: coordinator, customer, order-product, payment, delivery, policy, verifier. Với 50 case, trace cuối có đúng 350 dòng JSONL.

## 3. Vai trò, quyền truy cập và contract

| Agent | Quyền đọc | Handoff tạo ra | Giới hạn quyền |
| --- | --- | --- | --- |
| Coordinator | Case JSON, lookup đã nạp, handoff của agent | Output candidate | Không tự tạo fact ngoài handoff |
| CustomerAgent | `orders`, `customers` | Customer ID duy nhất và tối đa 5 related orders | Không đưa order lịch sử vào affected entities |
| OrderProductAgent | `order_items`, `products`, category translation | Item, seller, product và category theo thứ tự nguồn | Không sửa CSV; chỉ trả ID tồn tại |
| PaymentAgent | `order_items`, `order_payments` | Đối soát bằng `Decimal` | Không coi installment là payment row mới |
| DeliveryAgent | `orders`, `order_items` | Delivery/handoff variance | Không tạo tracking checkpoint |
| PolicyAgent | Handoff đã kiểm chứng | Issue, cause, responsibility, refund, actions | Chỉ dùng `EC_POLICY_V2`; unmatched case là lỗi |
| VerifierAgent | Candidate output | Pass hoặc exception | Không ghi output không hợp lệ |

Các handoff là object Python có cấu trúc và JSON-serializable. Thứ tự mảng được giữ theo thứ tự dòng nguồn; các giới hạn 5/3/20 được áp dụng ở biên output.

## 4. Luồng dữ liệu

1. Preflight kiểm tra đủ 9 CSV, 50 input, tên `case_id`, policy version và scope flags.
2. Loader stream từng CSV bằng `utf-8-sig`, tạo lookup theo khóa join để tránh quét lại file cho từng case.
3. Coordinator gọi các specialist agent và chuyển handoff cho PolicyAgent.
4. PolicyAgent áp dụng 6 primary issue theo thứ tự đề bài, sau đó thêm secondary issues và actions theo đúng thứ tự.
5. VerifierAgent kiểm tra schema đầy đủ, type, enum, rounding, null triplet, giới hạn, root-cause mapping, evidence format và quan hệ refund/status.
6. CLI ghi 50 JSON và thay mới `logging/trace.jsonl`.
7. Lệnh `verify` tái tính 50 case, so sánh output đã lưu, đồng thời đối chiếu entity/context/policy/refund/evidence trực tiếp với source data.
8. Lệnh `package` tạo `output.zip` ở root với đúng 50 JSON, tên entry từ `EC_001.json` đến `EC_050.json` và không có thư mục/file phụ.

## 5. Runtime và tái lập

- Model: `deterministic_ec_policy_v2`.
- Parameter size: 0; không dùng language model hay API provider.
- Framework: custom multi-agent rule engine bằng Python standard library.
- Runtime đã kiểm tra: Python 3.11.5 trên Windows.
- Model name được khai báo bằng hằng `MODEL_NAME` trong source và lặp lại trong metadata/trace.
- `.env` bị Git ignore; project không cần API key để chạy.

## 6. Lệnh vận hành

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
python -m dispute_resolution.cli preflight
python -m dispute_resolution.cli run
python -m dispute_resolution.cli verify
python -m dispute_resolution.cli package
```

`run` và `package` là deterministic: cùng source data và source code sẽ tạo cùng JSON và cùng SHA-256 cho `output.zip`.
