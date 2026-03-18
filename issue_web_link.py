import sys

from auth import is_authorized
from config import WEB_APP_PUBLIC_URL
from web_auth import issue_web_token


def main():
    if len(sys.argv) < 2:
        print("Usage: python issue_web_link.py <telegram_user_id>")
        raise SystemExit(1)

    user_id = int(sys.argv[1])
    if not is_authorized(user_id):
        print(f"User {user_id} is not authorized")
        raise SystemExit(2)

    if not WEB_APP_PUBLIC_URL:
        print("WEB_APP_PUBLIC_URL is not set")
        raise SystemExit(3)

    token = issue_web_token(user_id)
    print(f"{WEB_APP_PUBLIC_URL.rstrip('/')}/?token={token}")


if __name__ == "__main__":
    main()
