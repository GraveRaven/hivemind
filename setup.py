#!/usr/bin/env python

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

with open('requirements.txt') as f:
    required_packages = f.readlines()

setup(name='hivemind',
      version='0.1',
      description='A fork of Bees With Machine Guns to make it useful for more arbitrary tasks',
      author='Oscar Carlsson',
      author_email='',
      url='http://github.com/GraveRaven/hivemind',
      license='MIT',
      packages=['hivemind'],
      scripts=['hivemind'],
      install_requires=required_packages,
      classifiers=[
          'Environment :: Console',
          'Intended Audience :: Developers',
          'License :: OSI Approved :: MIT License',
          'Natural Language :: English',
          'Operating System :: OS Independent',
          'Programming Language :: Python',
          'Topic :: Software Development :: Testing :: Traffic Generation',
          'Topic :: Utilities',
          ],
     )
