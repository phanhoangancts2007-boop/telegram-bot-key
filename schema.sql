-- Chạy 1 lần trong Supabase: SQL Editor -> New query -> dán -> Run
-- Nếu đã tạo bảng kiểu cũ, xóa trước (sẽ mất dữ liệu trong các bảng này):
-- drop table if exists orders, keys, products, bot_users cascade;

create table if not exists products (
  id          text primary key,
  name        text not null,
  price       integer not null default 0,
  description text not null default '',
  sort_order  serial,                       -- thứ tự hiển thị
  created_at  timestamptz default now()
);

create table if not exists keys (
  id         bigserial primary key,
  product_id text not null references products(id) on delete cascade,
  key_code   text not null,
  is_sold    boolean not null default false,
  created_at timestamptz default now()
);
create index if not exists keys_product_sold_idx on keys(product_id, is_sold);

create table if not exists orders (
  id         bigserial primary key,
  user_id    bigint not null,
  product_id text references products(id) on delete set null,
  status     text not null default 'pending',   -- pending | completed | cancelled
  key_code   text,
  created_at timestamptz default now()
);

create table if not exists bot_users (
  user_id    bigint primary key,
  full_name  text,
  created_at timestamptz default now()
);

-- Nếu bảng products đã tồn tại mà chưa có cột sort_order, chạy dòng này:
-- alter table products add column if not exists sort_order serial;

-- Sản phẩm theo ảnh của bạn (chưa có key, hãy thêm bằng /addkey id key1 key2...)
insert into products (id, name, price, description) values
  ('acc_15xac',   'Acc 19-23ucl,19-25ty-ts , 18-24pl từ 15 xác tuyển chọn',        10000, 'Các acc được treo tool và đặt ngẫu nhiên'),
  ('acc_50xac',   'Acc 19-23ucl,19-25ty-ts , 18-24pl từ 50 xác',                   35000, 'Các acc được treo tool và đặt ngẫu nhiên nhiều hàng ngon fix giá 22u,23u'),
  ('acc_100xac',  'Acc 19-23ucl,19-25ty-ts , 18-24pl từ 100 xác siêu vip tuyển chọn', 85000, 'Chắc chắn nhiều hàng ngon như Bell,messi,kimic,kim min jea mùa 22ucl'),
  ('lienhe',      'Liên hệ: @xonnchamuser để tự chọn',                             10000, 'Liên hệ : @xonnchamuser để tự chọn'),
  ('acc_1b',      'Acc 1B và nhiều thẻ để mở',                                     30000, 'Acc 1B và nhiều thẻ để mở'),
  ('spam_1_7',    'Spam cầu thủ từ 1-7',                                           10000, 'Spam cầu thủ từ 1-7'),
  ('rues_23ucl',  'Rues 23ucl +9',                                                 100000,'Rues 23ucl +9'),
  ('dean_19s',    'Dean HenderSon 19S +8-11',                                      77777, 'Dean HenderSon 19S +8-11')
on conflict (id) do nothing;
