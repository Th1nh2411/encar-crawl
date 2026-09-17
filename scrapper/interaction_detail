// Trích xuất Car ID từ URL
const carid_match = input.url.match(/carid[=/](\d+)|\/detail\/(\d+)/);
if (!carid_match) {
  throw bad_input('Could not extract car ID from URL');
}
const carid = carid_match[1] || carid_match[2];
const detail_url = `https://fem.encar.com/cars/detail/${carid}`;

// Điều hướng tới trang chi tiết
try {
    navigate(detail_url, { wait_until: 'domcontentloaded', timeout: 30000 });
} catch (err) {
    console.log("Lỗi khi gọi navigate, thử tiếp tục xử lý trang hiện tại...");
}

// Sửa lại thành dùng CSS selector thay vì truyền số (chờ phần tử body xuất hiện)
wait('body'); 

// Kiểm tra xem trang có bị lỗi không
if (el_exists('.-z7xRIMV9Y') || el_exists('.error_page')) {
  collect({ status: "dead_page", url: detail_url });
} else {
  // Tiến hành parse dữ liệu bình thường
  collect(parse());
}