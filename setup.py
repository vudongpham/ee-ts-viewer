from setuptools import setup, find_packages

setup(
    name="eetsviewer",
    version="1.0",
    description="Web-based Sentinel-2 / Landsat time-series viewer powered by Google Earth Engine",
    packages=find_packages(exclude=["tests"]),
    install_requires=[
        "earthengine-api>=1.0",
        "Flask",
    ],
    package_data={
        "eetsviewer": [
            "templates/**/*",
            "static/**/*",
        ]
    },
    python_requires=">=3.11",
    entry_points={
        "console_scripts": [
            "eetsviewer=eetsviewer.app:main",
        ],
    },
)
