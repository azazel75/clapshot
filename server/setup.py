import setuptools
from setuptools import setup


with open("README.md", "r") as f:
    long_description = f.read()

with open('requirements.txt') as f:
    install_requires = f.read()


setup(
    name='clapshot-server',

    entry_points={
        'console_scripts': [
            'clapshot-server = clapshot_server.main:main'
        ],
    },
    data_files=[],

    version="0.1.0",
    author="Jarno Elonen",
    author_email="elonen@iki.fi",
    description="Backend server for Clapshot",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/elonen/clapshot",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.9',
    platforms='any',
    install_requires=install_requires
)
