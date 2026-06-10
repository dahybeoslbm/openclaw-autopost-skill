import argparse
parser = argparse.ArgumentParser()
parser.add_argument('user_prompt', nargs='*', default=[])
parser.add_argument('--topic', type=str, default="")
parser.add_argument('--platform', type=str, default="")
parser.add_argument('--time', type=str, default="")
args, unknown = parser.parse_known_args(["Đăng bài buôn mê lên fb tất cả các trang luôn", "--topic", "Buôn mê", "--platform", "facebook", "--time", ""])
print(args)
