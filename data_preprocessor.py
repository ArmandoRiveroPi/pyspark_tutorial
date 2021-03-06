from typing import List

import pandas
import pyspark.sql.functions as func
from pyspark.ml import Pipeline
from pyspark.ml.feature import StringIndexer, OneHotEncoder, VectorAssembler
from pyspark.sql import DataFrame


class DataPreprocessor(object):
    def __init__(self, train_df: DataFrame = None, test_df: DataFrame = None):
        assert train_df.schema == test_df.schema, "Train and test dataframes must have the same schema"
        self.train_df = train_df
        self.test_df = test_df
        self.train_encoded_df = None
        self.test_encoded_df = None

        self.one_hot_suffix = '_vec'
        self.indexed_suffix = "_cat"


    def explore_factors(self):
        """Generates a dictionary of one pandas dataframe per column
        containing all the columns values
        """
        factor_exploration = {}
        for column in self.factors:
            join_count = self._get_join_count(column)
            pandas_df = join_count.toPandas()
            pandas_df = pandas_df.set_index(column)
            factor_exploration[column] = self._binarize_pandas(pandas_df, 'in_test', 'in_train')
        return factor_exploration


    def explore_numeric_columns(self):
        exploration = {}
        for col in self.numeric_columns:
            train_summary = self.train_df.select(col).describe().withColumnRenamed(col, 'train')
            test_summary = self.test_df.select(col).describe().withColumnRenamed(col, 'test')
            pandas_df = train_summary.join(test_summary, ['summary'], how='outer').toPandas()
            exploration[col] = self._make_pandas_df_numeric(pandas_df.set_index('summary'))
        return exploration


    @staticmethod
    def print_exploration(exploration: dict):
        for column, df in exploration.items():
            print("COLUMN:", column, "=" * 15)
            print(df)
            print("=" * 50)


    def strip_columns(self, *columns, to_strip=" "):
        """Strip character ouf of string columns"""
        self._assert_are_factors(columns)
        strip_column_udf = func.udf(lambda x: x.strip(to_strip + " "))
        for column in columns:
            self.train_df = self.train_df.withColumn(column, strip_column_udf(column))
            self.test_df = self.test_df.withColumn(column, strip_column_udf(column))


    def string_index(self, *columns, suffix="_cat"):
        """
        Creates string indexed columns named as the original with appended suffix

        :param columns:
        :param suffix:
        :return:
        """
        self._assert_are_factors(columns)
        pipeline = Pipeline(stages=[StringIndexer(inputCol=factor, outputCol=factor + suffix) for factor in columns])
        self._fit_and_transform(pipeline)


    def one_hot_encode(self, *columns, suffix="_vec"):
        """
        Creates vectors of one-hot encoded columns from string indexed ones
        :param columns:
        :param suffix: it's appended to the output column
        :return:
        """
        encoder = OneHotEncoder(inputCols=columns, outputCols=[col + suffix for col in columns])
        self._fit_and_transform(encoder)


    def assemble_features(self, *columns, out_name='features'):
        v_assembler = VectorAssembler(inputCols=columns, outputCol=out_name)
        self._fit_and_transform(v_assembler)


    def prepare_to_model(self, target_col: str, to_strip=' '):
        """Runs all cleaning and encoding steps to generate
        dataframes ready to use in modeling"""
        # if target_col in self.factors:
        #     target_col += indexed_suffix
        self.strip_columns(*self.factors, to_strip=to_strip)
        self.string_index(*self.factors, suffix=self.indexed_suffix)
        # one-hot encode indexed factors, except target
        self.one_hot_encode(*self._one_hot_encode_columns(target_col), suffix=self.one_hot_suffix)
        # assemble all together with numeric columns into features (except target if it's numeric)
        self.assemble_features(*self._columns_to_assemble(target_col))

        if target_col in self.factors:
            target_col += self.indexed_suffix
        self.train_encoded_df = self._select_to_model(self.train_df, target_col)
        self.test_encoded_df = self._select_to_model(self.test_df, target_col)


    def _one_hot_encode_columns(self, target_col):
        return [fac + self.indexed_suffix for fac in self.factors if fac != target_col]


    def _columns_to_assemble(self, target_col):
        numeric = [col for col in self.numeric_columns
                   if col != target_col and not col.endswith(self.indexed_suffix)]
        one_hot_encoded = [col for col, data_type in self.train_df.dtypes
                           if self.indexed_suffix + self.one_hot_suffix in col]
        return numeric + one_hot_encoded


    @property
    def factors(self) -> List[str]:
        return self._get_cols_by_types(types=['string'])


    @property
    def numeric_columns(self) -> List[str]:
        return self._get_cols_by_types(types=['double', 'int'])


    def _assert_are_factors(self, columns):
        assert all(col in self.factors for col in columns), f"Some column in {columns} is not a factor"


    def _fit_and_transform(self, model):
        if hasattr(model, "fit"):
            model = model.fit(self.train_df)
        self.train_df = model.transform(self.train_df)
        self.test_df = model.transform(self.test_df)
        return model


    def _get_join_count(self, column=''):
        """
        Intended for factor exploration, does a group by in the test and train df
        and joins the result by value, hence exposing things like missing values in each
        of the dataframes
        """
        test_c = self.test_df.groupBy(column).count() \
            .orderBy('count', ascending=False).withColumn('in_test', func.col('count'))
        train_c = self.train_df.groupBy(column).count() \
            .orderBy('count', ascending=False).withColumn('in_train', func.col('count'))
        join_count = train_c.join(test_c, [column], how="outer").select(column, 'in_train', 'in_test')
        return join_count


    def _get_cols_by_types(self, types: List[str] = None):
        """Returns a list of columns in the self dataframes given a list of their types

        examples of types are 'int', 'double', and 'string'
        """
        train_cols = [col for col, data_type in self.train_df.dtypes if data_type in types]
        test_cols = [col for col, data_type in self.test_df.dtypes if data_type in types]
        assert train_cols == test_cols, "Columns in train and test df should be the same"
        return train_cols


    @staticmethod
    def _binarize_pandas(pandas_df, *cols):
        """
        Turns a pandas dataframe with numeric columns into 1s and 0s

        threshold is simply value > 0
        """
        for col in cols:
            pandas_df[col] = (pandas_df[col] > 0).astype(int)
        return pandas_df


    @staticmethod
    def _make_pandas_df_numeric(pandas_df: pandas.DataFrame):
        for col in pandas_df.columns:
            pandas_df[col] = pandas.to_numeric(pandas_df[col])
        return pandas_df


    @staticmethod
    def _select_to_model(df, target_col):
        return df.select(target_col, 'features').withColumnRenamed(target_col, 'label')
