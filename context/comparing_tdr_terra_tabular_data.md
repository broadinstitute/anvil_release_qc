# Considerations When Comparing TDR and Terra Tabular Data

# Terra Workspace Data Table Import Process

## General Behavior (Data Source Independent)

The Terra Data Table import process has the following behaviors that must be taken into account when performing comparisons of table and column counts.  
* Source tables with no rows are not created in the Terra Data Tables; they are ignored.  
* Source columns with no values are not created in the Terra Data Tables; they are ignored.  

The TDR snapshots commonly include some tables and columns defined by the data model that are not applicable to the content of a given dataset and are therefore empty.

The empty tables and columns must be removed from the TDR snapshot table and column counts before verifying that the Analysis Workspace Data Tables have at least as many as the TDR Snapshot.

----
