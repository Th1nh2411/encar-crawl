// Trang detail được server render sẵn: cả bản ghi xe nằm trong một khối JSON
// bên trong HTML. Lấy từ đó thay vì từ DOM, vì class name của Encar là hash
// (dd.NbZ1dmksTh, .md8SVev9rH) và đổi bất cứ lúc nào — DOM chỉ dùng làm dự phòng.

function json_blob() {
    let found = "";
    try {
        const scripts = $("script").toArray();
        for (let i = 0; i < scripts.length; i++) {
            const t = $(scripts[i]).text_sane();
            // Dừng ngay ở script đầu tiên có vehicleNo. Bản cũ nối tất cả
            // script khớp lại với nhau — trang SPA có bundle rất lớn, nên việc
            // đó tạo ra một chuỗi hàng MB rồi chạy regex 5 lần trên nó.
            if (t && t.indexOf("vehicleNo") !== -1) {
                found = t;
                break;
            }
        }
    } catch (e) {
        /* code worker không truy cập được script */
    }
    return found;
}

const json_src = json_blob();

// Một lượt duy nhất lấy mọi field cần, thay vì tạo RegExp mới cho từng key.
const fields = {};
if (json_src) {
    const keys = [
        "vehicleNo",
        "manufacturerName",
        "modelName",
        "gradeName",
        "gradeDetailName",
    ];
    const re = new RegExp('"(' + keys.join("|") + ')"\\s*:\\s*"([^"]*)"', "g");
    let m;
    while ((m = re.exec(json_src)) !== null) {
        // Giữ giá trị đầu tiên: bản ghi chính đứng trước các xe gợi ý.
        if (!fields[m[1]] && m[2]) fields[m[1]] = m[2];
    }
}

// JSON trước, DOM sau — ngược với bản cũ. Class name là hash nên hỏng thầm
// lặng khi Encar build lại; vehicleNo trong JSON là nguồn gốc của chính nó.
const car_number = fields.vehicleNo || $("dd.NbZ1dmksTh").text_sane() || null;

// Bản render bỏ mất tên hãng — nó hiện "3시리즈 (G20)320i M 스포츠" trong khi
// bản ghi cho "BMW 3시리즈 (G20) 320i M 스포츠".
let model_info =
    ["manufacturerName", "modelName", "gradeName", "gradeDetailName"]
        .map((k) => fields[k])
        .filter(Boolean)
        .join(" ") || null;

if (!model_info) model_info = $(".md8SVev9rH").text_sane() || null;

// `location` chỉ có trên browser worker; code worker nhận URL qua `input`.
let page_url = null;
try {
    page_url = location.href;
} catch (e) {
    /* code worker */
}
if (!page_url) page_url = input.url;

return {
    car_number: car_number,
    model_info: model_info,
    product_page_url: new URL(page_url),
};
