from setuptools import setup, find_packages

setup(
    name="llama-dashboard",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "flask",
        "huggingface_hub<0.33",
        "psutil",
        "tomli-w",
        "tomli; python_version < '3.11'",
    ],
)
