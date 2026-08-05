# Kiến trúc hệ thống — Olist Dispute Resolution

## 1. Mục tiêu

Hệ thống xử lý 50 case `EC_POLICY_V2` bằng kiến trúc hybrid multi-agent. Các agent dữ liệu đọc CSV, join bảng và tính toán bằng Python; Policy AI dùng `llama-3.1-8b-instant` (8B parameters) để chọn primary issue theo precedence. Policy catalog khai báo ánh xạ cause, responsibility, refund và actions; Verifier độc lập chặn mọi kết quả không khớp dữ liệu hoặc output schema.

Thiết kế này đáp ứng đồng thời hai yêu cầu: quyết định policy thật sự do model AI tạo ra, nhưng ID, tiền, timestamp và evidence không được model tự bịa.

## 2. Sơ đồ agent và handoff

```text
input/EC_XXX.json
        |
        v
Coordinator / CaseResolver
        |
        +--> CustomerAgent ------> customer identity + related orders
        +--> OrderProductAgent --> item/seller/product/category facts
        +--> PaymentAgent -------> Decimal totals + reconciliation
        +--> DeliveryAgent ------> delivery + seller handoff variance
        |                              |
        +------------------------------+
                       |
                       v
             PolicyAgent (Groq, Llama 3.1 8B)
               JSON primary classification
                       |
                       v
            Declarative Policy Catalog
              cause/party/refund/actions
                       |
                       v
                  VerifierAgent
            schema + source + policy audit
                 /                 \
                v                   v
      output/EC_XXX.json       trace.jsonl
```

Mỗi case có đúng 7 trace event: coordinator, customer, order-product, payment, delivery, policy và verifier. Lượt chạy 50 case tạo đúng 350 dòng và ghi đè trace cũ, không append.

## 3. Vai trò và quyền truy cập

| Agent | Dữ liệu được đọc | Handoff | Giới hạn quyền |
| --- | --- | --- | --- |
| Coordinator | Case input và handoff | Output candidate | Không tự tạo fact |
| CustomerAgent | Orders, customers | Customer ID, tối đa 5 related orders | Không đưa lịch sử vào affected entities |
| OrderProductAgent | Items, products | Item/seller IDs và product context | Chỉ dùng ID tồn tại trong CSV |
| PaymentAgent | Items, payments | Tổng tiền và trạng thái đối soát | Dùng `Decimal`, tolerance 0.10 BRL |
| DeliveryAgent | Orders, items | Delivery/handoff variance | Không tạo tracking checkpoint |
| PolicyAgent | Sáu condition flags đã kiểm chứng | JSON primary classification | Model không đọc file và không ghi output |
| Policy catalog | Primary và facts | Cause, parties, refund, secondary, actions | Lookup khai báo; không phân loại bằng nhánh `if/else` |
| VerifierAgent | Candidate và source lookup | Pass hoặc lỗi | Không sửa quyết định để che lỗi model |

## 4. Model và API

- Provider: Groq, dùng free API tier.
- Model: `llama-3.1-8b-instant`, đúng 8B parameters và không vượt giới hạn 10B.
- Model name nằm trong `src/dispute_resolution/ai_policy.py`, không nằm trong `.env`.
- `.env` chỉ chứa `GROQ_API_KEY` và đã được `.gitignore` loại khỏi Git.
- JSON Object Mode, temperature `0`, seed cố định và contract chỉ có một trường `primary` để model nhỏ hoạt động ổn định.
- Khoảng nghỉ 7 giây giữa các request để phù hợp hạn mức token/phút của free tier; lỗi 429/network được retry tối đa 3 lần.
- Client chỉ dùng Python standard library, không cần cài SDK ngoài.

## 5. Luồng chạy và tính toàn vẹn

1. `preflight` kiểm tra đủ 9 CSV và 50 input hợp lệ.
2. CSV được nạp một lần thành lookup; tiền dùng `Decimal`, timestamp giữ định dạng nguồn.
3. Bốn specialist tạo facts có cấu trúc.
4. PolicyAgent gửi sáu condition flags theo đúng precedence tới model 8B và nhận primary issue.
5. Policy catalog dựng các trường liên quan từ primary và facts; không dùng chuỗi `if/else` quyết định case.
6. Verifier kiểm schema, enum, ordering, entities, context, policy precedence, refund và evidence.
7. Chỉ khi cả 50 case pass, output, trace và metadata mới được thay thế. Lỗi giữa batch không phá bộ output cũ.
8. `verify` kiểm lại file đã lưu mà không gọi API lần hai.
9. `package` tạo `output.zip` với đúng `output/EC_001.json` đến `output/EC_050.json`, không kèm source, `.env`, trace hay metadata.

## 6. Lệnh vận hành

```powershell
Copy-Item .env.example .env
# Mở .env và điền GROQ_API_KEY
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
python -m dispute_resolution.cli preflight
python -m dispute_resolution.cli run
python -m dispute_resolution.cli verify
python -m dispute_resolution.cli package
```

`run` thực hiện 50 model calls thật. `verify` và `package` không tiêu tốn thêm API quota.
