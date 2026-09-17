// 1. Thiết lập URL cơ bản từ input hoặc mặc định
const base_url = input.url || 'https://www.encar.com/fc/fc_carsearchlist.do?carType=for#!%7B%22action%22%3A%22(And.Hidden.N._.(C.CarType.N._.(C.Manufacturer.BMW._.(C.ModelGroup.3%EC%8B%9C%EB%A6%AC%EC%A6%88._.Model.3%EC%8B%9C%EB%A6%AC%EC%A6%88%20(G20_).))))%22%2C%22toggle%22%3A%7B%7D%2C%22layer%22%3A%22%22%2C%22sort%22%3A%22ModifiedDate%22%2C%22page%22%3A1%2C%22limit%22%3A20%2C%22searchKey%22%3A%22%22%2C%22loginCheck%22%3Afalse%7D';

// Điều hướng tới trang mục tiêu
navigate(base_url, {wait_until: 'networkidle'});

// 2. Xử lý phân trang tự động (chỉ chạy ở lần chạy gốc đầu tiên)
if (!input.is_rerun) {
    const max_page = input.max_page || 5; // Bạn có thể tăng lên tuỳ nhu cầu
    
    for (let page_num = 2; page_num <= max_page; page_num++) {
        // Thay thế số trang trong cấu trúc JSON hash của Encar
        const page_url = base_url.replace('%22page%22%3A1', `%22page%22%3A${page_num}`);
        
        rerun_stage({
            url: page_url,
            max_page: max_page,
            is_rerun: true
        });
    }
}

// 3. Đợi vùng chứa danh sách xe 일반등록 load xong
const car_list_container = '#sr_normal';
try {
    wait(car_list_container, {timeout: 15000});
    scroll_to(car_list_container);
} catch (e) {
    console.log("Không tìm thấy vùng chứa danh sách xe, trang có thể đang tải chậm hoặc hết dữ liệu.");
}

// 4. Đợi các đường dẫn chi tiết xe xuất hiện
const car_link_selector = '#sr_normal tr td.inf a[href*="dc_cardetailview"]';
try {
    wait(car_link_selector, {timeout: 10000});
} catch (e) {
    console.log("Không tìm thấy link chi tiết xe nào trên trang này.");
}

// 5. Trích xuất danh sách URL chi tiết xe từ trang hiện tại
const { car_urls } = parse();

// 6. Đẩy các URL chi tiết sang stage tiếp theo để cào sâu hơn
if (car_urls && car_urls.length > 0) {
    for (let car_url of car_urls) {
        // Đảm bảo URL là tuyệt đối nếu nó là đường dẫn tương đối
        const full_car_url = car_url.startsWith('http') ? car_url : `https://www.encar.com${car_url}`;
        next_stage({ url: full_car_url });
    }
}