from setuptools import setup

setup(
    # Needed to silence warnings (and to be a worthwhile package)
    name='meiyume',
    url='https://github.com/travel-code-sleep/meiyume_master_source_codes',
    author='Amit Prusty',
    author_email='dcsamit@nus.edu.sg',
    # Needed to actually package something
    packages=['meiyume'],
    # Needed for dependencies
    install_requires=['numpy', 'logging', 'pandas', 'selenium', 
                      'missingno', 'matplotlib', 'tldextract',
                      'tqdm', 'plotly', 'seaborn', 'sklearn'],
    # *strongly* suggested for sharing
    version='0.5.1',
    # The license can be anything you like
    license='MIT',
    description='Contains all codes for data scraping and cleaning.',
    # We will also need a readme eventually (there will be a warning)
    long_description= open('README.md').read(),
)