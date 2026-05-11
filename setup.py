from setuptools import setup, find_packages

setup(
    name='toolspy',
    version='0.1.0',
    description='A collection of handy command-line tools',
    packages=find_packages(),
    python_requires='>=3.8',
    install_requires=[
        'lxml',
        'pillow',
        'python-docx',
        'typing_extensions',
    ],
    entry_points={
        'console_scripts': [
            'toolspy=tools:cli',
        ],
    },
)