import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "backend.app.main:app",
        host="127.0.0.1",
        port=3399,
        workers=1,
        env_file=".env",
    )
