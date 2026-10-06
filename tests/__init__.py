import logging

logging.getLogger("extractor").addHandler(logging.NullHandler())
logging.getLogger("extractor").propagate = False
