// Trích xuất Car ID từ URL
const carid_match = input.url.match(/carid[=/](\d+)|\/detail\/(\d+)/);
if (!carid_match) {
  throw bad_input('Could not extract car ID from URL');
}
const carid = carid_match[1] || carid_match[2];
const detail_url = `https://fem.encar.com/cars/detail/${carid}`;

// Trang detail được server render sẵn, bản ghi xe nằm trong HTML ngay từ
// response đầu tiên. Không cần chờ JS chạy xong, nên domcontentloaded là đủ —
// và không cần wait('body') sau đó, vì navigate đã trả về khi DOM sẵn sàng.
try {
    navigate(detail_url, { wait_until: 'domcontentloaded', timeout: 30000 });
} catch (err) {
    // Trang lỗi vẫn parse được (có thể là trang chặn), nên thử tiếp thay vì bỏ.
    console.log("navigate lỗi, thử parse trang hiện tại...");
}

// Xe đã gỡ tin trả về trang lỗi. Ghi lại như dead_page thay vì để parser trả
// về car_number rỗng — rỗng vì bị chặn và rỗng vì xe không còn là hai chuyện
// khác nhau.
if (el_exists('.-z7xRIMV9Y') || el_exists('.error_page')) {
  collect({ status: "dead_page", url: detail_url });
} else {
  collect(parse());
}
