function script_text() {
    let out = "";
    try {
        $("script")
            .toArray()
            .forEach((el) => {
                const t = $(el).text_sane();
                if (t && t.indexOf("vehicleNo") !== -1) out += t;
            });
    } catch (e) {
        /* no script access on this worker */
    }
    return out;
}

const json_src = script_text();

function from_json(key) {
    if (!json_src) return null;
    const m = json_src.match(new RegExp('"' + key + '"\\s*:\\s*"([^"]*)"'));
    return m && m[1] ? m[1] : null;
}

let car_number =
    $("dd.NbZ1dmksTh").text_sane() || from_json("vehicleNo") || null;

// The JSON is preferred for the model because the rendered field leaves the
// maker out -- it shows "3시리즈 (G20)320i M 스포츠" where the record gives
// "BMW 3시리즈 (G20) 320i M 스포츠".
let model_info =
    ["manufacturerName", "modelName", "gradeName", "gradeDetailName"]
        .map(from_json)
        .filter(Boolean)
        .join(" ") || null;

if (!model_info) model_info = $(".md8SVev9rH").text_sane() || null;

// `location` only exists on a browser worker; a code worker has the URL on
// `input` instead. Try both rather than assuming which one this runs on.
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
