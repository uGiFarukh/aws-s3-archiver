ARG PYTHON_VERSION=3.14
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION}
ARG PYTHON_VERSION

COPY requirements.txt ./
RUN python${PYTHON_VERSION} -m pip install --no-cache-dir --upgrade pip && \
	python${PYTHON_VERSION} -m pip install --no-cache-dir -r requirements.txt -t .

COPY app.py ./
RUN python${PYTHON_VERSION} -m compileall -q .

# Command can be overwritten by providing a different command in the template directly.
CMD ["app.lambda_handler"]
