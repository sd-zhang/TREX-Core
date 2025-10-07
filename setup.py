from setuptools import setup, find_packages

# ToDO, make nicer, for example by importing requirements or sth
setup(
    name='TREX_Core',
    version='5.0.0',
    install_requires=["asyncpg",
                      "commentjson",
                      "cuid2",
                      "databases",
                      "databases[postgresql]",
                      "gmqtt",
                      "packaging",
                      "psycopg[binary]",
                      "python-dateutil",
                      "pytz",
                      "python-rapidjson",
                      "sqlalchemy",
                      "sqlalchemy-utils",
                      "websockets",
                      "numpy",
                      "orjson"
                      ],
    packages=find_packages()
)
