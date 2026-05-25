from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="erpera_driver_app",
    version="0.0.1",
    description="Erpera Driver App ERP",
    author="Reformiqo",
    author_email="consultant.reformiqo@gmail.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
